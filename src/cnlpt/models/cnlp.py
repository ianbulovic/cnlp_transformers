"""
Module containing the CNLP transformer model.
"""

from __future__ import annotations

import inspect
import logging
import math
import random
from os import PathLike
from typing import Any, Union

import torch
from torch import nn
from torch.nn import CrossEntropyLoss, MSELoss
from transformers import AutoConfig, AutoModel
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_outputs import SequenceClassifierOutput
from transformers.modeling_utils import PreTrainedModel

from .. import __version__ as cnlpt_version

logger = logging.getLogger(__name__)


def generalize_encoder_forward_kwargs(encoder, **kwargs: Any) -> dict[str, Any]:
    """Create a new input feature argument that preserves only the features that are valid for this encoder.
    Warn if a feature is present but not valid for the encoder.

    Args:
        encoder: A HF encoder model

    Returns:
        Dictionary of valid arguments for this encoder
    """
    new_kwargs = dict()
    params = inspect.signature(encoder.forward).parameters
    for name, value in kwargs.items():
        if name not in params and value is not None:
            # Warn if a contentful parameter is not valid
            logger.warning(
                f"Parameter {name} not present for encoder class {encoder.__class__.__name__}."
            )
        elif name in params:
            # Pass all, and only, parameters that are valid,
            # regardless of whether they are None
            new_kwargs[name] = value
        # else, value is None and not in params, so we ignore it
    return new_kwargs


def freeze_encoder_weights(encoder, freeze: float):
    """Probabilistically freeze the weights of this HF encoder model according to the freeze parameter.
    Values of freeze >=1 are treated as if every parameter should be frozen.

    Args:
        encoder: HF encoder model
        freeze: Probability of freezing any given parameter (0-1)
    """
    for param in encoder.parameters():
        if freeze >= 1.0:
            param.requires_grad = False
        else:
            dart = random.random()
            if dart < freeze:
                param.requires_grad = False


class ClassificationHead(nn.Module):
    """Generic classification head that can be used for any task."""

    def __init__(self, config, num_labels, hidden_size=-1):
        super().__init__()
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.out_proj = nn.Linear(
            config.hidden_size if hidden_size < 0 else hidden_size, num_labels
        )

    def forward(self, features, *kwargs):
        x = self.dropout(features)
        x = self.out_proj(x)
        return x


class RepresentationProjectionLayer(nn.Module):
    """The class that maps from some output from a text encoder into a feature representation that can be classified.
    Project the representation to a new space depending on the task type, based on arguments passed in to the constructor."""

    def __init__(
        self,
        config: CnlpConfig,
        layer: int = 10,
        tokens: bool = False,
        tagger: bool = False,
        relations: bool = False,
        num_attention_heads: int = -1,
        head_size: int = 64,
    ):
        """
        Args:
            config: The config file for the encoder
            layer: Which layer to pull the encoder representation from
            tokens: Whether to classify an entity based on the token reprsentation rather than the CLS representation
            tagger: Whether the current task is a token tagging task
            relations: Whether the current task is relation exttraction
            num_attention_heads: For relations, how many "features" to use
            head_size: For relations, how big each head should be
        """
        super().__init__()
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        if relations:
            self.dense = nn.Identity()
        else:
            self.dense = nn.Linear(config.hidden_size, config.hidden_size)

        self.layer_to_use = layer
        self.tokens = tokens
        self.tagger = tagger
        self.relations = relations
        self.hidden_size = config.hidden_size

        if num_attention_heads <= 0 and relations:
            raise Exception(
                "Inconsistent configuration: num_attention_heads must be > 0 for relations"
            )

        if relations:
            self.num_attention_heads = num_attention_heads
            self.attention_head_size = head_size
            self.all_head_size = self.num_attention_heads * self.attention_head_size

            self.query = nn.Linear(config.hidden_size, self.all_head_size)
            self.key = nn.Linear(config.hidden_size, self.all_head_size)

        if tokens and (tagger or relations):
            raise Exception(
                "Inconsistent configuration: tokens cannot be true in tagger or relation mode"
            )

    def transpose_for_scores(self, x):
        # x: (batch x seq x all_head)

        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads,
            self.attention_head_size,
        )
        # (batch x seq x n_heads x head_size)
        x = x.view(*new_x_shape)

        # (batch x n_heads x seq x head_size)
        return x.permute(0, 2, 1, 3)

    def forward(self, features, event_tokens: torch.Tensor, **kwargs):
        # features: (layers x batch x seq x hidden)
        # event_tokens: (batch x seq)

        seq_length = features[0].shape[1]
        if self.tokens:
            # grab the average over the tokens of the thing we want to classify
            # probably involved passing in some sub-sequence of interest so we know what tokens to grab,
            # then we average across those tokens.

            # (batch)
            event_toks_per_seq = event_tokens.sum(1)

            # (batch x seq x hidden)
            expanded_tokens = event_tokens.unsqueeze(2).expand(
                features[0].shape[0], seq_length, self.hidden_size
            )

            # (batch x seq x hidden)
            filtered_features = features[self.layer_to_use] * expanded_tokens

            # (batch x hidden)
            x = filtered_features.sum(1) / event_toks_per_seq.unsqueeze(1).expand(
                features[0].shape[0], self.hidden_size
            )
        elif self.tagger:
            # (batch x seq x hidden)
            x = features[self.layer_to_use]
        elif self.relations:
            # something like multi-headed attention but without the weighted sum at the end, so i get (num_heads) features for each of N x N grid, which feads into NxN softmax (with the same parameters)
            # (batch x seq x hidden)
            hidden_states = features[self.layer_to_use]

            # (batch x n_heads x seq x head_size)
            key_layer = self.transpose_for_scores(self.key(hidden_states))
            # (batch x n_heads x seq x head_size)
            query_layer = self.transpose_for_scores(self.query(hidden_states))

            # (batch x n_heads x seq x seq)
            attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))

            # Now we have num_heads features for each N X N relations.
            x = attention_scores / math.sqrt(self.attention_head_size)
            # move the 12 dimension to the end for easier classification

            # (batch x seq x seq x n_heads)
            x = x.permute(0, 2, 3, 1)

        else:
            # take <s> token (equiv. to [CLS])
            # (batch x hidden)
            x = features[self.layer_to_use][..., 0, :]

        x = self.dropout(x)
        x = self.dense(x)
        x = torch.tanh(x)

        # for classification (including event tokens mode): (batch x hidden)
        # for tagging: (batch x seq x hidden)
        # for relations: (batch x seq x seq x n_heads)
        return x


class CnlpConfig(PretrainedConfig):
    """The config class for :class:`CnlpModelForClassification`."""

    model_type = "cnlpt"

    def __init__(
        self,
        *,
        encoder_name: Union[str, PathLike] = "roberta-base",
        finetuning_task: Union[list[str], None] = None,
        layer: int = -1,
        tokens: bool = False,
        num_rel_attention_heads: int = 12,
        rel_attention_head_dims: int = 64,
        tagger: dict[str, bool] = {},
        relations: dict[str, bool] = {},
        use_prior_tasks: bool = False,
        hier_head_config: Union[dict[str, Any], None] = None,
        label_dictionary: Union[dict[str, list[str]], None] = None,
        character_level: bool = False,
        **kwargs,
    ):
        """Create a new CnlpConfig object.

        Args:
            encoder_name: The encoder name to use with :meth:`transformers.AutoConfig.from_pretrained`. Defaults to "roberta-base".
            finetuning_task: The tasks for which this model is fine-tuned. Defaults to None.
            layer: The index of the encoder layer to extract features from. Defaults to -1.
            tokens: If true, sentence-level classification is done based on averaged token embeddings for token(s) surrounded by <e> </e> special tokens. Defaults to False.
            num_rel_attention_heads: The number of features/attention heads to use in the NxN relation classifier. Defaults to 12.
            rel_attention_head_dims: The number of parameters in each attention head in the NxN relation classifier. Defaults to 64.
            tagger: For each task, whether the task is a sequence tagging task. Defaults to {}.
            relations: For each task, whether the task is a relation extraction task. Defaults to {}.
            use_prior_tasks: Whether to use the outputs from the previous tasks as additional inputs for subsequent tasks. Defaults to False.
            hier_head_config: If this is a hierarchical model, this is where the config parameters go. Defaults to None.
            label_dictionary: A mapping from task names to label sets. Defaults to None.
            character_level: Whether the encoder is character level. Defaults to False.
        """
        super().__init__(**kwargs)
        # self.name_or_path='cnlpt'
        self.finetuning_task = finetuning_task
        self.layer = layer
        self.tokens = tokens
        self.num_rel_attention_heads = num_rel_attention_heads
        self.rel_attention_head_dims = rel_attention_head_dims
        self.tagger = tagger
        self.relations = relations
        self.use_prior_tasks = use_prior_tasks
        self.encoder_name = encoder_name
        self.encoder_config = AutoConfig.from_pretrained(encoder_name).to_dict()
        self.hier_head_config = hier_head_config
        self.label_dictionary = label_dictionary
        self.cnlpt_version = cnlpt_version
        self.character_level = character_level
        if encoder_name.startswith("distilbert"):
            self.hidden_dropout_prob = self.encoder_config["dropout"]
            self.hidden_size = self.encoder_config["dim"]
        elif self.encoder_config["model_type"] == "modernbert":
            self.hidden_size = self.encoder_config["hidden_size"]
            # downstream uses hidden dropout prob for additional layers, modernbert splits into different dropouts for different
            # parts of the encoder -- mlp dropout is probably generally good
            self.hidden_dropout_prob = self.encoder_config["mlp_dropout"]
            # don't need these in my code but keep them around just in case
            self.attention_dropout = self.encoder_config["attention_dropout"]
            self.embedding_dropout = self.encoder_config["embedding_dropout"]
            self.mlp_dropout = self.encoder_config["mlp_dropout"]
            self.classifier_dropout = self.encoder_config["classifier_dropout"]
        else:
            try:
                self.hidden_dropout_prob = self.encoder_config["hidden_dropout_prob"]
                self.hidden_size = self.encoder_config["hidden_size"]
            except KeyError as ke:
                raise ValueError(
                    f"Encoder config does not have an attribute"
                    f' "{ke.args[0]}"; this is likely because the API of'
                    f" the chosen encoder differs from the BERT/RoBERTa"
                    f" API and the DistilBERT API. Encoders with different"
                    f" APIs are not yet supported (#35)."
                )


class CnlpModelForClassification(PreTrainedModel):
    """The CNLP transformer model."""

    base_model_prefix = "cnlpt"
    config_class = CnlpConfig

    def __init__(
        self,
        config: config_class,
        *,
        class_weights: Union[dict[str, float], None] = None,
        final_task_weight: float = 1.0,
        freeze: float = -1.0,
        bias_fit: bool = False,
    ):
        """Create a new CNLP transformer model instance from a config object.

        Args:
            config: The CnlpConfig object that configures this model
            class_weights: If provided, the weights to use for each task when computing the loss. Defaults to None.
            final_task_weight: The weight to use for the final task when computing the loss. Defaults to 1.0.
            freeze: What proportion of encoder weights to freeze (-1 for none). Defaults to -1.0.
            bias_fit: Whether to fine-tune only the bias of the encoder. Defaults to False.
        """
        super().__init__(config)

        encoder_config = AutoConfig.from_pretrained(config.encoder_name)
        encoder_config.vocab_size = config.vocab_size
        config.encoder_config = encoder_config.to_dict()
        encoder_model = AutoModel.from_config(encoder_config)

        self.encoder = encoder_model.from_pretrained(config.encoder_name)

        # part of the motivation for leaving this
        # logic alone for character level models is that
        # at the time of writing,  CANINE and Flair are the only game in town.
        # CANINE's hashable embeddings for unicode codepoints allows for
        # additional parameterization, which rn doesn't seem so relevant
        if not config.character_level:
            self.encoder.resize_token_embeddings(
                encoder_config.vocab_size, mean_resizing=False
            )
        # This would seem to be redundant with the label list, which maps from tasks to labels,
        # but this version is ordered. This will allow the user to specify an order for any methods
        # where we feed the output of one task into the next.
        # It also will be used as the canonical order of returning results/logits
        self.tasks = config.finetuning_task

        if config.layer > self.num_layers:
            raise ValueError(
                f"The layer specified ({config.layer}) is too big for the specified encoder which has {self.num_layers} layers"
            )

        if freeze > 0:
            freeze_encoder_weights(self.encoder, freeze)

        if bias_fit:
            for name, param in self.encoder.named_parameters():
                if "bias" not in name:
                    param.requires_grad = False

        self.feature_extractors = nn.ModuleDict()
        self.classifiers = nn.ModuleDict()

        total_prev_task_labels = 0
        for task_name, task_labels in config.label_dictionary.items():
            task_num_labels = len(task_labels)
            self.feature_extractors[task_name] = RepresentationProjectionLayer(
                config,
                layer=config.layer,
                tokens=config.tokens,
                tagger=config.tagger[task_name],
                relations=config.relations[task_name],
                num_attention_heads=config.num_rel_attention_heads,
                head_size=config.rel_attention_head_dims,
            )
            if config.relations[task_name]:
                hidden_size = config.num_rel_attention_heads
                if config.use_prior_tasks:
                    hidden_size += total_prev_task_labels

                self.classifiers[task_name] = ClassificationHead(
                    config, task_num_labels, hidden_size=hidden_size
                )
            else:
                self.classifiers[task_name] = ClassificationHead(
                    config, task_num_labels
                )
            total_prev_task_labels += task_num_labels

        # Are we operating as a sequence classifier (1 label per input sequence) or a tagger (1 label per input token in the sequence)
        self.tagger = config.tagger
        self.relations = config.relations

        if class_weights is None:
            self.class_weights = {x: None for x in config.label_dictionary.keys()}
        else:
            self.class_weights = class_weights

        self.label_dictionary = config.label_dictionary
        self.final_task_weight = final_task_weight
        self.use_prior_tasks = config.use_prior_tasks
        self.reg_temperature = 1.0

        # self.init_weights()

    @property
    def num_layers(self):
        if self.encoder.config.model_type == "modernbert":
            return len(self.encoder.base_model.layers)
        else:
            return len(self.encoder.encoder.layer)

    def predict_relations_with_previous_logits(
        self, features: torch.Tensor, logits: list[torch.Tensor]
    ) -> torch.Tensor:
        """For the relation prediction task, use previous predictions of the tagging task as additional features in the
        representation used for making the relation prediction.

        Args:
            features: The existing feature vector for the relations
            logits: The predicted logits from the tagging task

        Returns:
            The augmented feature tensor
        """

        # features is (batch x seq x seq x n_heads)
        seq_len = features.shape[1]
        for prior_task_logits in logits:
            if len(features.shape) == 4:
                if len(prior_task_logits.shape) == 3:
                    # prior task is sequence tagging:
                    # we have batch x seq x num_classes.
                    # we want to concatenate the num_classes to the variables at each element of the sequence,
                    # but then need to broadcast it down all the rows of the matrix.
                    aug = prior_task_logits.unsqueeze(
                        2
                    )  # add another dimension to repeat along
                    aug = aug.repeat(
                        1, 1, seq_len, 1
                    )  # repeat along the new empty dimension so we have our seq logits repeated seq_len x seq_len
                    features = torch.cat(
                        (features, aug), 3
                    )  # concatenate the  relation matrix with the sequence matrix
                else:
                    logger.warning(
                        f"It is not implemented to add a task of shape {prior_task_logits.shape!s} to a relation matrix"
                    )
            elif len(features.shape) == 3:
                # sequence
                logger.warning(
                    "It is not implemented to add previous task of any type to a sequence task"
                )

        return features

    def compute_loss(
        self,
        task_logits: torch.FloatTensor,
        labels: torch.LongTensor,
        task_ind: int,
        task_num_labels: int,
        batch_size: int,
        seq_len: int,
        state: dict,
    ) -> None:
        """
        Computes the loss for a single batch and a single task.

        Args:
            task_logits:
            labels:
            task_ind:
            task_num_labels:
            batch_size:
            seq_len:
            state:
        :meta private:
        """
        task_name = self.tasks[task_ind]
        if task_num_labels == 1:
            #  We are doing regression
            loss_fct = MSELoss()
            task_loss = loss_fct(task_logits.view(-1), labels.view(-1))
        else:
            if self.class_weights[task_name] is not None:
                class_weights = torch.FloatTensor(self.class_weights[task_name]).to(
                    self.device
                )
            else:
                class_weights = None
            loss_fct = CrossEntropyLoss(weight=class_weights)

            if self.relations[task_name]:
                task_labels = labels[
                    :, :, state["task_label_ind"] : state["task_label_ind"] + seq_len
                ]
                state["task_label_ind"] += seq_len
                task_loss = loss_fct(
                    task_logits.permute(0, 3, 1, 2),
                    task_labels.type(torch.LongTensor).to(labels.device),
                )
            elif self.tagger[task_name]:
                # in cases where we are only given a single task the HF code will have one fewer dimension in the labels, so just add a dummy dimension to make our indexing work:
                if labels.ndim == 2:
                    task_labels = labels
                elif labels.ndim == 3:
                    # labels = labels.unsqueeze(1)
                    task_labels = labels[:, :, state["task_label_ind"]]
                else:
                    task_labels = labels[:, 0, state["task_label_ind"], :]

                state["task_label_ind"] += 1
                task_loss = loss_fct(
                    task_logits.view(-1, task_num_labels),
                    task_labels.reshape(
                        [
                            batch_size * seq_len,
                        ]
                    )
                    .type(torch.LongTensor)
                    .to(labels.device),
                )
            else:
                if labels.ndim == 1:
                    task_labels = labels
                elif labels.ndim == 2:
                    task_labels = labels[:, task_ind]
                elif labels.ndim == 3:
                    task_labels = labels[:, 0, task_ind]
                else:
                    raise NotImplementedError(
                        "Have not implemented the case where a classification task "
                        "is part of an MTL setup with relations and sequence tagging"
                    )

                state["task_label_ind"] += 1
                task_loss = loss_fct(
                    task_logits, task_labels.type(torch.LongTensor).to(labels.device)
                )

        if state["loss"] is None:
            state["loss"] = task_loss
        else:
            task_weight = (
                1.0 if task_ind + 1 < len(self.tasks) else self.final_task_weight
            )
            state["loss"] += task_weight * task_loss

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        event_tokens=None,
    ):
        r"""Forward method.

        Args:
            input_ids (`torch.LongTensor` of shape `(batch_size, sequence_len)`, *optional*):
                A batch of chunked documents as tokenizer indices.
            attention_mask (`torch.LongTensor` of shape `(batch_size, sequence_len)`, *optional*):
                Attention masks for the batch.
            token_type_ids (`torch.LongTensor` of shape `(batch_size, sequence_len)`, *optional*):
                Token type IDs for the batch.
            position_ids: (`torch.LongTensor` of shape `(batch_size, sequence_len)`, *optional*):
                Position IDs for the batch.
            head_mask (`torch.LongTensor` of shape `(num_heads,)` or `(num_layers, num_heads)`, *optional*):
                Token encoder head mask.
            inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_len, hidden_size)`, *optional*):
                A batch of chunked documents as token embeddings.
            labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
                Labels for computing the sequence classification/regression loss.
                Indices should be in :obj:`[0, ..., config.num_labels - 1]`.
                If :obj:`config.num_labels == 1` a regression loss is computed (Mean-Square loss),
                If :obj:`config.num_labels > 1` a classification loss is computed (Cross-Entropy).
            output_attentions (`bool`, *optional*): Whether or not to return the attentions tensors of all attention layers.
            output_hidden_states: not used.
            event_tokens: a mask defining which tokens in the input are to be averaged for input to classifier head; only used when self.tokens==True.

        Returns: (`transformers.SequenceClassifierOutput`) the output of the model
        """

        kwargs = generalize_encoder_forward_kwargs(
            self.encoder,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=True,
            return_dict=True,
        )

        outputs = self.encoder(input_ids, **kwargs)

        batch_size, seq_len = input_ids.shape

        logits = []

        state = dict(loss=None, task_label_ind=0)

        for task_ind, task_name in enumerate(self.tasks):
            task_labels = self.label_dictionary[task_name]
            # hidden_states has shape (layers x batch x seq x hidden)

            # features shape:
            # for classification (including event tokens mode): (batch x hidden)
            # for tagging: (batch x seq x hidden)
            # for relations: (batch x seq x seq x n_heads)
            features = self.feature_extractors[task_name](
                outputs.hidden_states, event_tokens
            )
            if self.use_prior_tasks:
                # note: this specific way of incorporating previous logits doesn't help in my experiments with thyme/clinical tempeval
                if self.relations[task_name]:
                    features = self.predict_relations_with_previous_logits(
                        features, logits
                    )
            task_logits = self.classifiers[task_name](features)
            logits.append(task_logits)

            if labels is not None:
                self.compute_loss(
                    task_logits,
                    labels,
                    task_ind,
                    len(task_labels),
                    batch_size,
                    seq_len,
                    state,
                )

        if self.training:
            return SequenceClassifierOutput(
                loss=state["loss"],
                logits=logits,
                hidden_states=outputs.hidden_states,
                attentions=outputs.attentions,
            )
        else:
            return SequenceClassifierOutput(
                loss=state["loss"], logits=logits, attentions=outputs.attentions
            )

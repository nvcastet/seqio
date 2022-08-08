# Copyright 2022 The SeqIO Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MetricValue objects to wrap results being returned by metric funcitons."""

import dataclasses
import enum
import inspect
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

import clu.metrics
import flax
import numpy as np
from seqio.vocabularies import Vocabulary
from t5.evaluation import metrics as t5_metrics
import tensorflow.compat.v2 as tf


@dataclasses.dataclass
class MetricValue:
  """A base method for the dataclasses that represent tensorboard values.

  Task `metric_fn`s should output `Mapping[str, MetricValue]` which will be
  written by a `Logger`.
  """


@dataclasses.dataclass
class Scalar(MetricValue):
  """The default tensorflow value, used for creating time series graphs."""
  value: Union[int, float]


@dataclasses.dataclass
class Text(MetricValue):
  """Text to output to tensorboard, markdown is rendered by tensorboard."""
  textdata: Union[str, bytes]


@dataclasses.dataclass
class Image(MetricValue):
  """An image to output to tensorboard.

  The format for the image array should match the format expected for the data
  parameter described
  [here](https://www.tensorflow.org/api_docs/python/tf/summary/image).
  """
  image: np.ndarray
  max_outputs: int = 3


@dataclasses.dataclass
class Audio(MetricValue):
  """An audio example to output to tensorboard.

  The format for the audio array should match the format expected for the data
  parameter described
  [here](https://www.tensorflow.org/api_docs/python/tf/summary/audio).
  """
  audiodata: np.ndarray
  sample_rate: int = 44100
  max_outputs: int = 3


@dataclasses.dataclass
class Histogram(MetricValue):
  """A histogram to output to tensorboard."""
  values: np.ndarray
  bins: Optional[int] = None


@dataclasses.dataclass
class Generic(MetricValue):
  """A raw tensor to output to tensorboard."""
  tensor: np.ndarray
  metadata: tf.compat.v1.SummaryMetadata


@dataclasses.dataclass(frozen=True)
class Feature:
  """A container for attributes of output features of data providers."""
  vocabulary: Vocabulary
  add_eos: bool = True
  required: bool = True
  dtype: tf.DType = tf.int32
  rank: int = 1


class ModelOutputType(enum.Enum):
  """Model output types."""
  PREDICTION = 1
  SCORE = 2
  PREDICTION_WITH_AUX = 3
  SCORE_WITH_INTERMEDIATES = 4

  @classmethod
  def to_str(cls, enm):
    return {
        cls.PREDICTION: "prediction",
        cls.SCORE: "score",
        cls.PREDICTION_WITH_AUX: "prediction_with_aux",
        cls.SCORE_WITH_INTERMEDIATES: "score_with_intermediates",
    }[enm]

MetricFnCallable = Callable[..., Mapping[str, Union[MetricValue, float]]]


@flax.struct.dataclass
class Squad(clu.metrics.Metric):
  """Implements SQuAD metrics, maximizing over answers per question."""

  metric_fn_kwargs: Dict[str, Any]
  model_output_type: ModelOutputType = ModelOutputType.PREDICTION

  @classmethod
  def from_model_output(
      cls,
      inputs: Sequence[Mapping[str, Any]],
      model_output: Union[np.ndarray, Tuple[np.ndarray, np.ndarray]],
      features: Mapping[str, Feature],
      target_field_name: str = "targets") -> clu.metrics.Metric:

    # Postprocesses the targets here.
    postprocessed_targets = []
    for ex in inputs:
      postprocessed_targets.append(
          [tf.compat.as_text(a) for a in ex["answers"]])
    kwargs = {"targets": postprocessed_targets}

    # Decodes the predictions here.
    vocab = features["targets"].vocabulary
    predictions = [vocab.decode(tokens) for tokens in model_output]

    kwargs["predictions"] = predictions

    return cls(metric_fn_kwargs=kwargs)

  def compute(self):
    return t5_metrics.squad(**self.metric_fn_kwargs)


@flax.struct.dataclass
class LegacyMetricClu(clu.metrics.Metric):
  """Metric class for legacy use-case where metric fn is supplied."""

  _metric_fn: MetricFnCallable
  _postprocess_fn: Callable[..., Any]
  model_output_type: ModelOutputType
  metric_fn_kwargs: Dict[str, Any]

  @classmethod
  def empty(cls, metric_fn, postprocess_fn) -> clu.metrics.Metric:
    pos_args = tuple(
        key
        for key, param in inspect.signature(metric_fn).parameters.items()
        if param.default == inspect.Parameter.empty)
    if pos_args == ("targets", "scores"):
      model_output_type = ModelOutputType.SCORE
    elif pos_args == ("targets", "predictions"):
      model_output_type = ModelOutputType.PREDICTION
    elif pos_args == ("targets", "predictions", "aux_values"):
      model_output_type = ModelOutputType.PREDICTION_WITH_AUX
    else:
      raise ValueError(
          "Metric functions must have positional arguments matching either "
          "('targets', 'scores'), ('targets', 'predictions') or "
          "('targets', 'predictions', 'aux_values'). "
          f"Got: {pos_args}")

    return cls(_metric_fn=metric_fn, _postprocess_fn=postprocess_fn,
               model_output_type=model_output_type, metric_fn_kwargs={})

  def postprocess_fn(self, targets_or_predictions: Any,
                     **postprocess_kwargs) -> Any:
    """Applies the postprocessing to targets or predictions."""
    if self._postprocess_fn:
      return self._postprocess_fn(targets_or_predictions, **postprocess_kwargs)
    return targets_or_predictions

  def from_model_output(
      self,
      inputs: Sequence[Mapping[str, Any]],
      model_output: Union[np.ndarray, Tuple[np.ndarray, np.ndarray]],
      features: Mapping[str, Feature],
      target_field_name: str = "targets") -> clu.metrics.Metric:

    # Postprocesses the targets here.
    postprocessed_targets = []
    for ex in inputs:
      pretokenized_target_field_name = target_field_name + "_pretokenized"
      if pretokenized_target_field_name in ex:
        target = ex[pretokenized_target_field_name]
      else:
        target = features[target_field_name].vocabulary.decode(
            [int(x) for x in ex[target_field_name]])
      if isinstance(target, bytes):
        target = target.decode("utf-8")

      postprocessed_targets.append(
          self.postprocess_fn(target, example=ex, is_target=True))

    self.metric_fn_kwargs["targets"] = postprocessed_targets

    if self.model_output_type == ModelOutputType.SCORE:
      self.metric_fn_kwargs["scores"] = model_output
    else:
      vocab = features["targets"].vocabulary
      if self.model_output_type == ModelOutputType.PREDICTION_WITH_AUX:
        self.metric_fn_kwargs["aux_values"] = model_output[1]
        predictions = [vocab.decode(tokens) for tokens in model_output[0]]
      elif self.model_output_type == ModelOutputType.PREDICTION:
        predictions = [vocab.decode(tokens) for tokens in model_output]

      # Postprocesses the predictions here.
      postprocessed_predictions = [
          self.postprocess_fn(p, example=ex, is_target=False)
          for ex, p in zip(inputs, predictions)
      ]

      self.metric_fn_kwargs["predictions"] = postprocessed_predictions

    return self

  def compute(self):
    return self._metric_fn(**self.metric_fn_kwargs)


# Copyright 2021 The SeqIO Authors.
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

"""Classes for logging evaluation metrics and inference results."""

import abc
import base64
import itertools
import json
import os
import time
from typing import Any, Mapping, Optional, Sequence, Type

from absl import logging
import numpy as np
from seqio import metrics as metrics_lib
import tensorflow as tf
import tensorflow_datasets as tfds


class Logger(abc.ABC):
  """Abstract base class for logging.

  Attributes:
    output_dir: a directory to save the logging results (e.g., TensorBoard
      summary) as well as the evaluation results (e.g., "inputs_pretokenized",
      "target_pretokenize" and "prediction").
  """

  def __init__(self, output_dir):
    self.output_dir = output_dir

  @abc.abstractmethod
  def __call__(self, task_name: str, step: int,
               metrics: Mapping[str, metrics_lib.MetricValue],
               dataset: tf.data.Dataset, inferences: Mapping[str,
                                                             Sequence[Any]],
               targets: Sequence[Any]) -> None:
    """Logs the metrics and inferences for each task.

    Args:
      task_name: The name of the task these datapoints are relevant to.
      step: The timestep to place this datapoint at.
      metrics: A mapping from series names to numeric datapoints to be added to
         that series.
      dataset: The Task dataset.
      inferences: Mapping from inference type ("predictions", "scores") to the
        model outputs, aligned with the dataset.
      targets: The postprocessed targets, aligned with the dataset.
    """
    ...


class TensorBoardLogger(Logger):
  """A logger that writes metrics to TensorBoard summaries."""

  def __init__(self, output_dir: str):
    """TensorBoardLogger initializer.

    Args:
      output_dir: The base directory where all logs will be written.
    """
    super().__init__(output_dir)
    self._summary_writers = {}

  def _get_summary_writer(self, task_name: str) -> tf.summary.SummaryWriter:
    """Create (if needed) and return a SummaryWriter for a given task."""
    if task_name not in self._summary_writers:
      with tf.compat.v1.Graph().as_default():
        self._summary_writers[task_name] = tf.compat.v1.summary.FileWriter(
            os.path.join(self.output_dir, task_name))
    return self._summary_writers[task_name]

  def __call__(self,
               task_name: str,
               step: int,
               metrics: Mapping[str, metrics_lib.Scalar],
               dataset: tf.data.Dataset,
               inferences: Mapping[str, Sequence[Any]],
               targets: Sequence[Any]) -> None:
    """Log the eval results and optionally write summaries for TensorBoard.

    Note:
      This is the default implementation using tensorflow v1 operations. This
      only supports logging metrics of the Scalar type.

    Args:
      task_name: The name of the task these datapoints are relevant to.
      step: The timestep to place this datapoint at.
      metrics: A mapping from series names to numeric datapoints to be added to
         that series.
      dataset: The Task dataset, which is unused by this logger.
      inferences: The model outputs, which are unused by this logger.
      targets: The postprocessed targets, which are unused by this logger.
    """
    del dataset
    del inferences
    del targets
    if step is None:
      logging.warning("Step number for the logging session is not provided. "
                      "A dummy value of -1 will be used.")
      step = -1

    summary_writer = self._get_summary_writer(task_name)

    for metric_name, metric_value in metrics.items():
      if not isinstance(metric_value, metrics_lib.Scalar):
        raise ValueError(f"Value for metric '{metric_name}' should be of "
                         f"type 'Scalar, got '{type(metric_value).__name__}'.")
      summary = tf.compat.v1.Summary()

      tag = f"eval/{metric_name}"
      logging.info("%s at step %d: %.3f", tag, step, metric_value.value)

      summary.value.add(tag=tag, simple_value=metric_value.value)
      summary_writer.add_summary(summary, step)

    summary_writer.flush()


class TensorAndNumpyEncoder(json.JSONEncoder):
  """JSON Encoder to use when encoding dicts with tensors and numpy arrays."""

  def __init__(self, *args, max_ndarray_size=32, **kwargs):
    self.max_ndarray_size = max_ndarray_size
    super().__init__(*args, **kwargs)

  def default(self, obj):
    if isinstance(obj, tf.Tensor):
      if obj.dtype == tf.bfloat16:
        # bfloat16 not supported, convert to float32.
        obj = tf.cast(obj, tf.float32)
      obj = obj.numpy()

    if isinstance(obj, np.ndarray):
      obj_dtype = obj.dtype
      if str(obj.dtype) == "bfloat16":
        # bfloat16 not supported, convert to float32.
        obj = obj.astype(np.float32)
      if obj.size <= self.max_ndarray_size:
        return obj.tolist()  # Convert arrays to lists of py-native types.
      else:
        # If the ndarray is larger than allowed, return a summary string
        # instead of the entire array.
        first_five_str = str(obj.reshape([-1])[:5].tolist())[1:-1]
        return (
            f"{type(obj).__name__}(shape={obj.shape}, dtype={obj_dtype}); "
            f"first: {first_five_str} ...")
    elif (np.issubdtype(type(obj), np.number) or
          np.issubdtype(type(obj), np.bool_)):
      return obj.item()  # Convert most primitive np types to py-native types.
    elif hasattr(obj, "dtype") and obj.dtype == tf.bfloat16.as_numpy_dtype:
      return float(obj)
    elif isinstance(obj, bytes):
      # JSON doesn't support bytes. First, try to decode using utf-8 in case
      # it's text. Otherwise, just base64 encode the bytes.
      try:
        return obj.decode("utf-8")
      except UnicodeDecodeError:
        return base64.b64encode(obj)

    return json.JSONEncoder.default(self, obj)


class JSONLogger(Logger):
  """A logger that writes metrics and model outputs to JSONL files."""

  def __init__(
      self,
      output_dir: str,
      write_n_results: Optional[int] = None,
      json_encoder_cls: Type[json.JSONEncoder] = TensorAndNumpyEncoder):
    """JSONLogger constructor.

    Args:
      output_dir: The base directory where all logs will be written.
      write_n_results: number of scores/predictions to be written to the file at
        each step. If None, scores and predictions from all examples are
        written.
      json_encoder_cls: Class to use for serializing JSON to file.
    """
    super().__init__(output_dir)
    self._write_n_results = write_n_results
    self._json_encoder_cls = json_encoder_cls

  def __call__(self,
               task_name: str,
               step: int,
               metrics: Mapping[str, metrics_lib.MetricValue],
               dataset: tf.data.Dataset,
               inferences: Mapping[str, Sequence[Any]],
               targets: Sequence[Any]) -> None:
    if step is None:
      logging.warning("Step number for the logging session is not provided. "
                      "A dummy value of -1 will be used.")
      step = -1

    metrics_fname = os.path.join(self.output_dir, f"{task_name}-metrics.jsonl")

    serializable_metrics = {}
    for metric_name, metric_value in metrics.items():
      if isinstance(metric_value, metrics_lib.Scalar):
        serializable_metrics[metric_name] = metric_value.value
      elif isinstance(metric_value, metrics_lib.Text):
        serializable_metrics[metric_name] = metric_value.textdata
      else:
        logging.warning(
            "Skipping JSON logging of non-serializable metric '%s' of type %s.",
            metric_name, type(metric_value))

    if metrics:
      logging.info("Appending metrics to %s", metrics_fname)
      # We simulate an atomic append for filesystems that do not suppport
      # mode="a".
      file_contents = ""
      if tf.io.gfile.exists(metrics_fname):
        with tf.io.gfile.GFile(metrics_fname, "r") as f:
          file_contents = f.read()
      with tf.io.gfile.GFile(metrics_fname + ".tmp", "w") as f:
        f.write(file_contents)
        f.write(json.dumps({"step": step, **serializable_metrics}) + "\n")
      tf.io.gfile.rename(metrics_fname + ".tmp", metrics_fname, overwrite=True)

    if self._write_n_results == 0:
      return

    write_tick = time.time()
    inferences_fname = os.path.join(self.output_dir,
                                    f"{task_name}-{step:06}.jsonl")
    logging.info("Writing inferences to %s", inferences_fname)
    with tf.io.gfile.GFile(inferences_fname, "w") as f:
      examples_with_scores = itertools.zip_longest(
          tfds.as_numpy(dataset), inferences.get("predictions", []),
          targets, inferences.get("scores", []))
      if self._write_n_results:
        examples_with_scores = itertools.islice(
            examples_with_scores, 0, self._write_n_results)

      for inp, prediction, target, score in examples_with_scores:

        # tfds.as_numpy does not convert ragged tensors
        for k in inp:
          if isinstance(inp[k], tf.RaggedTensor):
            inp[k] = inp[k].numpy()

        json_dict = {"input": inp}

        # Only write `prediction` if it is JSON serializable.
        if prediction is not None:
          try:
            json.dumps(prediction, cls=self._json_encoder_cls)
            json_dict["prediction"] = prediction
          except TypeError:
            logging.warning("`prediction` is not JSON serializable",
                            exc_info=True)

        # Only write `target` if it is JSON serializable.
        try:
          json.dumps(target, cls=self._json_encoder_cls)
          json_dict["target"] = target
        except TypeError:
          logging.warning("`target` is not JSON serializable", exc_info=True)

        if score is not None:
          json_dict["score"] = score

        json_str = json.dumps(json_dict, cls=self._json_encoder_cls)
        f.write(json_str + "\n")
    write_time = time.time() - write_tick
    logging.info("Writing completed in %02f seconds (%02f examples/sec).",
                 write_time,
                 len(inferences) / write_time)
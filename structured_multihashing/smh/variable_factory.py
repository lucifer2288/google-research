# coding=utf-8
# Copyright 2021 The Google Research Authors.
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

"""Implementation of VariableFactory.

VariableFactory class maintains a virtual weight store which are
generated by the summation of several kronecker products. These virtual weights
will be allocated to specific model through a custom getter method passed to
tf.get_variable method via tf.variable_scope.
"""

from __future__ import absolute_import
from __future__ import division

from __future__ import print_function

import abc
import collections
import re
import numpy as np
import six

import tensorflow.compat.v1 as tf
from typing import Any, Dict, Text

from structured_multihashing.smh import variable_pool as vp

SUPPORTED_DTYPES = [tf.bfloat16, tf.float16, tf.float32, tf.float64]
FACTORY_SCOPE_NAME = 'SMH'

logging = tf.compat.v1.logging


@six.add_metaclass(abc.ABCMeta)
class VariableFactoryAbstract(object):
  """Abstract class to support various variable factories."""

  def __init__(self, apply_to, modifier = None):
    """Creates an instance of `VariableFactory`.

    Args:
      apply_to: string, a regular expression to match variables that should be
        allocated from the kronecker variable pool. If set to None, the
        kronecker weights will be applied to every variable created in the
        model.
      modifier: A function that modifies the virtual variable before being
        returned by the getter. The modifier function accepts the tf.Tensor as
        its first arguments, a list (*args) and a dict (**kwargs) passed from
        _variable_creator and _custom_getter.
    """
    self._apply_to_regex = apply_to or '.*'
    self._modifier = modifier
    self._layer_name_variable_index_map = collections.defaultdict(list)
    self._allocs = {}

  @property
  def layer_name_variable_index_map(self):
    return self._layer_name_variable_index_map

  # This is not py3 compatible. Change this when upgrading.
  @abc.abstractproperty
  def custom_getter(self):
    pass

  # This is not py3 compatible. Change this when upgrading.
  @abc.abstractproperty
  def custom_getter_tf2(self):
    pass

  def _log_name(self, name, message):
    self._layer_name_variable_index_map[name].append(message)

  def _allocate_tensor(self, name, tensor):
    self._allocs[name] = tensor

  def _get_weights_shape(self, kwargs):
    """Gets shape information for weights to allocate from kwargs.

    If kwargs['shape'] is not None, kwargs['shape'] will be returned.
    If kwargs['shape'] is None, we will get the shape information from
    kwargs['initial_value']. kwargs['initial_value'] can be a tensor object or
    a callable object with no arguments that returns a tensor object as the
    initial value. In both of these two cases, we will get the shape information
    from the tensor object that serves as the initial value.

    Args:
      kwargs: A dict object that includes the keyword args passed to the
        variable custom getter function.

    Returns:
      a `tf.TensorShape` object represents the shape of the weights to be
      allocated.
    """
    if kwargs['shape'] is not None:
      return kwargs['shape']
    else:
      if 'initial_value' not in kwargs:
        raise ValueError(
            '`initial_value` is not in kwargs: cannot infer the shape.')
      elif callable(kwargs['initial_value']):
        initial_tensor = kwargs['initial_value']()
      else:
        initial_tensor = kwargs['initial_value']
      # Check whether initial_tensor is None before returning its shape.
      if initial_tensor is None:
        raise ValueError('Cannot get shape information from kwargs: ', kwargs)

      return initial_tensor.shape

  def use_custom_getter(self, var_name):
    return re.match(self._apply_to_regex,
                    var_name) and FACTORY_SCOPE_NAME not in var_name

  def _apply_modifier(self, tensor):
    return self._modifier(tensor) if self._modifier else tensor


class VariableFactory(VariableFactoryAbstract):
  """Factory class to allocate kronecker weights to TensorFlow models."""

  def __init__(self,
               variable_pool,
               apply_to,
               modifier = None):
    """Creates an instance of `VariableFactory`.

    Args:
      variable_pool: A VariablePool object or None if regular getter to be used.
      apply_to: string, a regular expression to match variables that should be
        allocated from the kronecker variable pool. If set to None, the
        kronecker weights will be applied to every variable created in the
        model.
      modifier: A function that modifies the virtual variable before being
        returned by the getter. The modifier function accepts the tf.Tensor as
        its first arguments, a list (*args) and a dict (**kwargs) passed from
        _variable_creator and _custom_getter.
    """
    self._variable_pool = variable_pool
    super(VariableFactory, self).__init__(apply_to, modifier=modifier)

  @property
  def custom_getter(self):
    """Returns a custom getter function for tf.variable_scope().

    The details about custom getter for tf.variable_scope can be found at:
    https://www.tensorflow.org/api_docs/python/tf/get_variable.

    The getter only support dtype in `SUPPORTED_DTYPES`.

    Returns:
      A getter function. The getter will raise ValueError for unsupported types.
    """
    if not self._variable_pool:
      return lambda getter, *args, **kwargs: getter(*args, **kwargs)

    def _custom_getter(getter, name, shape, *args, **kwargs):
      """Custom getter function for tf.variable_scope."""
      if not self.use_custom_getter(name):
        # the given variable name does not match the regular expression. Use the
        # default getter function.
        logging.info(
            'Variable : %s does not match the given regular expression: %s.'
            ' Using TensorFlow default getter.', name, self._apply_to_regex)
        self._log_name(name, 'default')
        return getter(name, shape, *args, **kwargs)

      if kwargs['dtype'] not in SUPPORTED_DTYPES:
        raise ValueError('dtype must be one of the TensorFlow float type. '
                         'Received: %s' % kwargs['dtype'])
      if name in self._allocs:  # TODO(elade): make statement more robust.
        logging.info(
            'Variable : %s has been alocated! returning previous tensor slice.',
            name)
        return self._allocs[name]

      return self._get_tensor(shape, name)

    return _custom_getter

  @property
  def custom_getter_tf2(self):
    """Returns a custom getter function for TF2/Keras model.

    The returned getter function will be used together with
    tf.variable_creator_scope to create variables for TF2/Keras models. Details
    can be found at
    https://www.tensorflow.org/api_docs/python/tf/variable_creator_scope.

    Returns:
      a getter method with signature:
          def variable_creator(next_creator, **kwargs).
    """
    if not self._variable_pool:
      return lambda next_creator, **kwargs: next_creator(**kwargs)

    def _variable_creator(next_creator, **kwargs):
      """Custom variable creator method."""
      name = kwargs['name']
      if not self.use_custom_getter(name):
        # the given variable name does not match the regular expression. Use the
        # default getter function.
        logging.info(
            'Variable %s does not match the given regular expression: %s. '
            'Using TensorFlow default getter.', name, self._apply_to_regex)
        self._log_name(name, 'TF2_default')
        return next_creator(**kwargs)

      if kwargs['dtype'] not in SUPPORTED_DTYPES:
        raise ValueError('dtype must be one of the TensorFlow float type. '
                         'Received: %s' % kwargs['dtype'])
      # TODO(b/144939898): Solve reuse variable problem for TF2 interace.
      # if name in self._allocs:
      #  logging.info(
      #      'Seems like {} was allocated, returning allocated var.'.format(
      #          name))
      # return self._allocs[name]
      return self._get_tensor(self._get_weights_shape(kwargs), name)

    return _variable_creator

  def _get_tensor(self, shape, name):
    """Allocates variables from self._variable_pool, with given name."""
    msg = 'Allocating virtual variable: `{}` with shape {}'.format(name, shape)
    logging.info(msg)
    tensor = self._variable_pool.get_slice(shape)
    tensor = tf.identity(self._apply_modifier(tensor), name)
    self._allocate_tensor(name, tensor)
    return tensor


def _get_fanout_scale(shape):
  """Returns a float which scales the virtual weights."""
  if len(shape) == 4:
    kernel_height, kernel_width, _, out_filters = shape
    fan_out = int(kernel_height * kernel_width * out_filters)
    return np.float32(np.sqrt(2.0 / fan_out))
  elif len(shape) == 2:
    _, output_dim = shape
    # TODO(elade): Verify that the scale is equal to fanout initilizer's.
    return np.float32(1.0 / np.sqrt(int(output_dim)))
  else:
    logging.warn('Shape %s not of rank != 2,4 scaling with 1.0', shape)
    return np.float32(1.0)


def constant_fanout_scale_modifier(tensor):
  """Returns the scale for conv and dense layers for efficent net.

  Args:
    tensor: The tf.Tensor to compute scale for.

  Returns:
    Float value, the scale.
  """
  scale = tf.constant(
      value=_get_fanout_scale(tensor.shape),
      dtype=tensor.dtype,
      name=tensor.op.name + 'fanout_scale')
  _assert_not_float64(scale)
  return tf.multiply(tensor, scale, name='constant_fanout_scale_modifier')


def variable_fanout_scale_modifier(tensor):
  """Returns the scale for conv and dense layers for efficent net.

  Creates a variable initilized to have the scale of fanout. The variable will
  be named `KOMPRESS/%TENSOR_NAME%/scale`.

  Args:
    tensor: The tf.Tensor to compute scale for.

  Returns:
    Float value, the scale.
  """
  scaled_tensor = constant_fanout_scale_modifier(tensor)
  _assert_not_float64(scaled_tensor)
  with tf.variable_scope(FACTORY_SCOPE_NAME):
    name_prefix = tensor.op.name
    scale = tf.get_variable(
        name='%s/scale' % name_prefix,  # remove :0 from tensor name.
        shape=(),
        dtype=tensor.dtype,
        trainable=True,
        initializer=tf.constant_initializer(1.0, dtype=tensor.dtype))
    _assert_not_float64(scale)
    var_scaled_tensor = tf.multiply(
        scaled_tensor, scale, name='variable_fanout_scale_modifier')
    _assert_not_float64(var_scaled_tensor)
    return var_scaled_tensor


def variable_scale_modifier(tensor):
  """Returns the scale for conv and dense layers for efficent net.

  Creates a variable initilized to have the scale of fanout. The variable will
  be named `KOMPRESS/%TENSOR_NAME%/vscale`.

  Args:
    tensor: The tf.Tensor to compute scale for.

  Returns:
    Float value, the scale.
  """
  dtype = tf.float32
  with tf.variable_scope(FACTORY_SCOPE_NAME):
    name_prefix = tensor.op.name
    scale = tf.get_variable(
        name='%s/vscale' % name_prefix,  # remove :0 from tensor name.
        shape=(),
        dtype=dtype,
        trainable=True,
        initializer=tf.constant_initializer(1.0, dtype=dtype))

    return tf.multiply(tensor, scale, name='vscale_modifier')


def relu_modifier(tensor):
  return tf.nn.relu(tensor, name='relu_modifier')


def modifier_factory(modifier_list):

  def _modifier(tensor):
    for fn in modifier_list:
      tensor = fn(tensor)
    return tensor

  return _modifier


def sign_fw_modifier(tensor):
  """Modifies tensor to be +-1 on forward and identity backward."""
  tensor = tensor + tf.stop_gradient(tf.sign(tensor) - tensor)
  return tf.identity(tensor, name='sign_fw_modifier')


SUPPORTED_MODIFIERS = {
    'FANOUT': constant_fanout_scale_modifier,
    'VAR_FANOUT': variable_fanout_scale_modifier,
    'VAR': variable_scale_modifier,
    'RELU': relu_modifier,
    'BIN': sign_fw_modifier
}


def _assert_not_float64(tensor):
  # TODO(elade): Remove: this should not be a concern, why arn't we in control?
  assert tensor.dtype != tf.float64

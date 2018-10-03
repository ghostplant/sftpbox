#!/usr/bin/env python3

import tensorflow as tf
from tensorflow.python.client import device_lib
import numpy as np
import warnings, time, os


def warn(*args, **kwargs):
    pass
warnings.warn = warn

tf_weights = dict()

def init_weight(shape):
  if len(shape) == 2:
    fan_in, fan_out = shape[0], shape[1]
  elif len(shape) == 4:
    fan_in, fan_out = shape[0] * shape[1] * shape[2], shape[0] * shape[1] * shape[3]
  limit = np.sqrt(6.0 / (fan_in + fan_out))
  scope = tf.get_default_graph().get_name_scope()
  if scope not in tf_weights:
    tf_weights[scope] = []
  seed = len(tf_weights[scope])
  var = tf.Variable(tf.random_uniform(shape, -limit, limit, seed=seed))
  tf_weights[scope].append(var)
  return var


def conv2d(X, filter, ksize, stride, padding=0):
  if padding != 0:
    X = tf.pad(X, tf.constant([[0, 0], [0, 0], [padding, padding], [padding, padding]]))
  return tf.nn.conv2d(X, init_weight([ksize, ksize, int(X.shape[1]), filter]), strides=[1, 1, stride, stride], data_format='NCHW', padding='VALID')

def dense(X, out_channels):
  return tf.matmul(X, init_weight([int(X.shape[1]), out_channels])) + tf.Variable(tf.zeros([out_channels]))

def mpool2d(X, ksize, stride):
  return tf.nn.max_pool(X, ksize=[1, 1, ksize, ksize], strides=[1, 1, stride, stride], padding='VALID', data_format='NCHW')

def apool2d(X, ksize, stride):
  return tf.nn.avg_pool(X, ksize=[1, 1, ksize, ksize], strides=[1, 1, stride, stride], padding='VALID', data_format='NCHW')

def lrn(X):
  return tf.nn.lrn(X, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75)

def flatten(X):
  return tf.reshape(X, shape=[-1, np.product(X.shape[1:])])


data_dir, batch_size, depth, height, width, n_classes = '/tmp/dataset/catsdogs', 64, 3, 224, 224, 2

if data_dir[-1] != '/':
  data_dir += '/'

if not os.path.exists(data_dir + ".success"):
  try:
    dataset = os.path.basename(data_dir[:-1])
    print('Downloading dataset %s ..' % dataset)
    assert(0 == os.system("mkdir -p '%s' && curl -L 'https://github.com/ghostplant/lite-dnn/releases/download/lite-dataset/images-%s.tar.gz' | tar xzvf - -C '%s' >/dev/null" % (data_dir, dataset, data_dir)))
    open('/tmp/dataset/%s/.success' % dataset, 'wb').close()
  except:
    raise Exception("Failed to download dataset.")


def generator():
  from keras.preprocessing.image import ImageDataGenerator
  from keras.utils import OrderedEnqueuer
  gen = ImageDataGenerator(data_format='channels_first', rescale=1./255, fill_mode='nearest').flow_from_directory(
                           data_dir + '/train', target_size=(height, width), batch_size=batch_size)
  enqueuer = OrderedEnqueuer(gen, use_multiprocessing=False)
  enqueuer.start(workers=8)
  n_classes = gen.num_classes

  while True:
    batch_xs, batch_ys = next(enqueuer.get())
    yield batch_xs, batch_ys

def create_imagenet_resnet50v1(X, Y):
  print('Creating imagenet_resnet50v1 on scope `%s`..' % tf.get_default_graph().get_name_scope())

  X = conv2d(X, 64, 7, 2, 3)
  X = tf.nn.relu(X)
  X = mpool2d(X, 3, 2)

  def bottleneck_block_v1(X, depth, depth_bottleneck, stride):
    shortcut = None
    if depth == int(X.shape[1]):
      if stride == 1:
        shortcut = X
      else:
        shortcut = mpool2d(X, 1, 2)
    else:
      shortcut = conv2d(X, depth, 1, stride)
      shortcut = tf.nn.relu(shortcut)
    output = conv2d(X, depth_bottleneck, 1, stride)
    output = tf.nn.relu(output)
    output = conv2d(output, depth_bottleneck, 3, 1, 1)
    output = tf.nn.relu(output)
    output = conv2d(output, depth, 1, 1)
    return tf.nn.relu(output + shortcut)

  layer_counts = [3, 4, 6, 3]
  for i in range(layer_counts[0]):
    X = bottleneck_block_v1(X, 256, 64, 1)
  for i in range(layer_counts[1]):
    X = bottleneck_block_v1(X, 512, 128, 2 if i == 0 else 1)
  for i in range(layer_counts[2]):
    X = bottleneck_block_v1(X, 1024, 256, 2 if i == 0 else 1)
  for i in range(layer_counts[3]):
    X = bottleneck_block_v1(X, 2048, 512, 2 if i == 0 else 1)

  X = apool2d(X, int(X.shape[2]), 1)
  X = flatten(X)
  X = dense(X, int(Y.shape[1]))

  loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits_v2(logits=X, labels=Y))
  return loss

train_ops, losses = [], []
grad_avg, opts = [], []
tower_grads, tower_vars = [], []

ngpus = 4 # len(get_available_gpus())

for i in range(ngpus):
  with tf.name_scope('gpu_%d' % i), tf.device('/gpu:%d' % i):
    ds = tf.data.Dataset.from_generator(generator, (tf.float32, tf.float32))
    ds = ds.prefetch(buffer_size=batch_size)
    images, labels = ds.make_one_shot_iterator().get_next()
    X = tf.reshape(images, (-1, depth, height, width))
    Y = tf.reshape(labels, (-1, n_classes))

    ''' with tf.device('/cpu:0'):
      X = tf.zeros([batch_size, depth, height, width], dtype=tf.float32)
      Y = tf.zeros([batch_size, n_classes], dtype=tf.float32) '''

    # opt = tf.train.MomentumOptimizer(learning_rate=0.01, momentum=0.9)
    # opt = tf.train.AdamOptimizer(learning_rate=0.01)
    # opt = tf.train.AdadeltaOptimizer(learning_rate=0.01)
    opt = tf.train.AdagradOptimizer(learning_rate=0.01)

    loss = create_imagenet_resnet50v1(X, Y)
    grad_gv = opt.compute_gradients(loss, tf_weights[tf.get_default_graph().get_name_scope()])
    grad_g = [g for g, _ in grad_gv]
    grad_v = [v for _, v in grad_gv]
    tower_grads.append(grad_g)
    tower_vars.append(grad_v)
    losses.append(loss)
    opts.append(opt)

if ngpus == 1:
  it = next(zip(*tower_grads))
  grad_avg = [it]
else:
  for it in zip(*tower_grads):
    all_sum = tf.contrib.nccl.all_sum(it)
    for i in range(len(all_sum)):
      all_sum[i] /= ngpus
    grad_avg.append(all_sum)

for i, it in enumerate(zip(*grad_avg)):
  with tf.name_scope('gpu_%d' % i), tf.device('/gpu:%d' % i):
    grad = zip(list(it), tower_vars[i])
    train_ops.append(opts[i].apply_gradients(grad))

mean_loss = tf.reduce_sum(losses) / ngpus


config=tf.ConfigProto(allow_soft_placement=True)
config.gpu_options.allow_growth=True

with tf.Session(config=config) as sess:
  sess.run(tf.global_variables_initializer())

  # Synchronize initial weights
  print('Warnup Variables ..')
  sess.run(train_ops)

  print('Launch Training ..')
  freq = 100
  init_time = last_time = time.time()
  for k in range(100000):
    sess.run(train_ops)
    if (k + 1) % freq == 0:
      curr_time = time.time()
      print('step = %d (batch = %d; %.2f images/sec): loss = %.4f, during = %.3fs' % ((k + 1),
        batch_size * ngpus, batch_size * ngpus * (k + 1) / (curr_time - init_time), sess.run(mean_loss), curr_time - last_time))

      last_time = time.time()

import numpy as np
import jax
from jax import numpy as jnp
import ml_collections

from flax import linen as nn
from flax.training import train_state
from flax import serialization
import optax


class InitialModule(nn.Module):
    """Initial reconstruction module
    """
    @nn.compact
    def __call__(self, x):
        x = nn.Conv(features=64, kernel_size=(11,), padding='CAUSAL')(x)
        x = nn.relu(x)
        x = nn.Conv(features=32, kernel_size=(11,), padding='CAUSAL')(x)
        x = nn.relu(x)
        x = nn.Conv(features=1, kernel_size=(11,), padding='CAUSAL')(x)
        return x


class SecondaryModule(nn.Module):
    """Secondary reconstruction module"""


    @nn.compact
    def __call__(self, carry, x):
        # reshaping
        x = jnp.squeeze(x)
        # LSTM
        carry, h  = nn.OptimizedLSTMCell()(carry, x)
        # h = nn.tanh(h)
        # dense layer
        h = nn.Dense(256)(h)
        # reshaping
        h = jnp.expand_dims(h, 2)
        return carry, h

    @staticmethod
    def initialize_carry(batch_dims, hidden_size):
        return nn.OptimizedLSTMCell.initialize_carry(
            jax.random.PRNGKey(0), batch_dims, hidden_size)


class CSNet(nn.Module):
    """CS Net for ECG"""
    hidden_size: int = 250

    @nn.compact
    def __call__(self, x):
        batch_size = x.shape[0]
        batch_dims = (batch_size, )
        x = InitialModule()(x)
        initial_state = SecondaryModule.initialize_carry(batch_dims, self.hidden_size)
        _, h = SecondaryModule()(initial_state, x)
        return h


def get_config(epochs=200, batch_size=256):
  """Get the default hyperparameter configuration."""
  config = ml_collections.ConfigDict()

  # config.learning_rate = 0.1
  # config.momentum = 0.9
  config.learning_rate = 0.0005
  config.batch_size = batch_size
  config.num_epochs = epochs
  return config

@jax.jit
def apply_model(state, X_input, X_true):
  """Computes gradients, loss for a single batch."""
  def loss_fn(params):
    X_est = state.apply_fn({'params': params}, X_input)
    x_diff = X_est - X_true
    loss = jnp.mean(x_diff * x_diff) / 2.0
    return loss, X_est

  grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
  (loss, X_est), grads = grad_fn(state.params)
  return grads, loss

@jax.jit
def update_model(state, grads):
  return state.apply_gradients(grads=grads)


def create_train_state(rng, X, config):
    """Creates initial `TrainState`."""
    model = CSNet()
    params = model.init(rng, X)['params']
    tx = optax.adam(config.learning_rate)
    return train_state.TrainState.create(
      apply_fn=model.apply, params=params, tx=tx)


def train_epoch(state, X_risen, X_true, batch_size, rng):
  """Train for a single epoch."""
  train_ds_size = X_true.shape[0]
  steps_per_epoch = train_ds_size // batch_size

  perms = jax.random.permutation(rng, train_ds_size)
  perms = perms[:steps_per_epoch * batch_size]  # skip incomplete batch
  perms = perms.reshape((steps_per_epoch, batch_size))

  epoch_loss = []

  for perm in perms:
    batch_input = X_risen[perm, ...]
    batch_expected = X_true[perm, ...]
    grads, loss = apply_model(state, batch_input, batch_expected)
    state = update_model(state, grads)
    epoch_loss.append(loss)
  train_loss = np.mean(epoch_loss)
  return state, train_loss



def train_and_evaluate(Phi, X, Y, codec_params, config):
    X_risen = Y @ Phi / codec_params.d
    n  = codec_params.n

    # normalization procedure
    x_mean = jnp.mean(X_risen, axis=0)
    X_risen = X_risen  - x_mean
    X = X - x_mean
    x_std = jnp.std(X_risen, axis=0)
    X_risen = X_risen / x_std
    X = X / x_std

    X_true = jnp.expand_dims(X, 2)
    X_risen = jnp.expand_dims(X_risen, 2)
    print(X_true.shape, X_risen.shape)

    rng = jax.random.PRNGKey(0)

    ## train validation split
    n_total = X_risen.shape[0]
    n_validation = n_total // 8
    n_training = n_total - n_validation

    rng, split_rng = jax.random.split(rng)
    perms = jax.random.permutation(split_rng, n_total)
    train_idx = perms[:n_training]
    valid_idx = perms[n_training:]
    X_true_train = X_true[train_idx, ...]
    X_risen_train = X_risen[train_idx, ...]
    X_true_validation = X_true[valid_idx, ...]
    X_risen_validation = X_risen[valid_idx, ...]


    # initialize the network
    rng, init_rng = jax.random.split(rng)
    shape = (1, n, 1)
    dummy_x = jnp.empty(shape)
    state = create_train_state(init_rng, dummy_x, config)
    # print(jax.tree_util.tree_map(lambda x: x.shape, state.params))

    # perform training
    for epoch in range(1, config.num_epochs + 1):
        rng, input_rng = jax.random.split(rng)
        state, train_loss = train_epoch(state, X_risen_train, X_true_train,
            config.batch_size,
            input_rng)

        _, validation_loss = apply_model(state, X_risen_validation,
                                              X_true_validation)

        print(f'epoch:{epoch}, train_loss: {train_loss:.2e}, validation_loss: {validation_loss:.2e}')

    # return the final trained model
    return {'params' : state.params, 'mean': x_mean, 'std': x_std} 


def save_to_disk(result, file_path_base):
    params = result['params']
    bytes_output = serialization.to_bytes(params)
    file_path = f'{file_path_base}.mdl'
    with open(file_path, 'wb') as f:
        f.write(bytes_output)
        f.close()
    x_mean = result['mean']
    x_std = result['std']
    mean = np.asarray(mean)
    std = np.asarray(std)
    combined = np.concatenate((mean, std))
    file_path = f'{file_path_base}.npy'
    np.save(file_path, combined)


def load_from_disk(file_path_base, n):
    shape = (1, n, 1)
    x = jnp.empty(shape)
    model = CSNet()
    rng = jax.random.PRNGKey(0)
    params = model.init(rng, x)['params']
    file_path = f'{file_path_base}.mdl'
    with open(file_path, 'rb') as f:
        bytes_output = f.read()
        f.close()
        params = serialization.from_bytes(params, bytes_output)
    file_path = f'{file_path_base}.npy'
    combined = np.load(file_path)
    mean = combined[:n]
    std = combined[n:]
    return model, {'params' : params, 'mean': mean, 'std': std}


def predict(net, net_params, Phi, Y, d):
    params = net_params['params']
    x_mean = net_params['mean']
    x_std = net_params['std']
    X_risen = Y @ Phi / d

    X_risen = X_risen  - x_mean
    X_risen = X_risen / x_std

    X_risen = jnp.expand_dims(X_risen, 2)
    X_est = net.apply({'params': params}, X_risen)

    # denormalize
    X_est = jnp.squeeze(X_est)
    X_est = X_est * x_std
    X_est = X_est + x_mean

    return X_est


def test_loss(net, net_params, Phi, X, Y, d):
    params = net_params['params']
    x_mean = net_params['mean']
    x_std = net_params['std']

    X_risen = Y @ Phi / d

    X_risen = X_risen  - x_mean
    X = X - x_mean
    X_risen = X_risen / x_std
    X = X / x_std

    X_true = jnp.expand_dims(X, 2)
    X_risen = jnp.expand_dims(X_risen, 2)
    # print(X_true.shape, X_risen.shape)
    X_est = net.apply({'params': params}, X_risen)
    x_diff = X_est - X_true
    # scale down
    x_diff = x_diff
    loss = jnp.mean(x_diff * x_diff) / 2.0
    print(f'Test loss: {loss:.3e}')

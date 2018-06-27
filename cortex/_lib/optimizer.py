'''Module for setting up the optimizer.

'''

import logging

import torch
import torch.optim as optim
import torch.backends.cudnn as cudnn

from . import exp, reg


__author__ = 'R Devon Hjelm'
__author_email__ = 'erroneus@gmail.com'

logger = logging.getLogger('cortex.optimizer')
OPTIMIZERS = {}

_optimizer_defaults = dict(
    SGD=dict(momentum=0.9),
    Adam=dict(betas=(0.5, 0.999))
)


def setup(  # noqa C901
        model,
        optimizer='Adam',
        learning_rate=1.e-4,
        clipping={},
        weight_decay={},
        l1_decay={},
        optimizer_options={},
        model_optimizer_options={}):
    '''Optimizer entrypoint.

    Args:
        optimizer: Optimizer type. See `torch.optim` for supported optimizers.
        learning_rate: Learning rate.
        updates_per_routine: Updates per routine.
        clipping: If set, this is the clipping for each model.
        weight_decay: If set, this is the weight decay for specified model.
        l1_decay: If set, this is the l1 decay for specified model.
        optimizer_options: Optimizer options.
        model_optimizer_options: Optimizer options for specified model.

    '''

    model_optimizer_options = model_optimizer_options or {}
    weight_decay = weight_decay or {}
    l1_decay = l1_decay or {}
    clipping = clipping or {}

    # Set the optimizer options
    if len(optimizer_options) == 0:
        optimizer_options = 'default'
    if optimizer_options == 'default'\
            and optimizer in _optimizer_defaults.keys():
        optimizer_options = _optimizer_defaults[optimizer]
    elif optimizer_options == 'default':
        raise ValueError(
            'Default optimizer options for'
            ' `{}` not available.'.format(optimizer))

    # initialize regularization
    reg.init(clipping=clipping, weight_decay=l1_decay)

    # Set the optimizers
    if callable(optimizer):
        op = optimizer
    elif hasattr(optim, optimizer):
        op = getattr(optim, optimizer)
    else:
        raise NotImplementedError(
            'Optimizer not supported `{}`'.format(optimizer))

    for network_key, network in model.nets.items():
        # Set model parameters to cpu or gpu
        network.to(exp.DEVICE)
        # TODO(Devon): is the next line really doing anything?
        if str(exp.DEVICE) == 'cpu':
            pass
        else:
            torch.nn.DataParallel(
                network, device_ids=range(
                    torch.cuda.device_count()))

    model.data.reset(make_pbar=False, mode='test')
    model.eval_step()

    training_nets = model._get_training_nets()

    for network_key in set(training_nets):
        logger.info('Building optimizer for {}'.format(network_key))
        network = model.nets[network_key]

        if isinstance(network, (tuple, list)):
            params = []
            for net in network:
                params += list(net.parameters())
        else:
            params = list(network.parameters())

        # Needed for reloading.
        for p in params:
            p.requires_grad = True

        # Learning rates
        if isinstance(learning_rate, dict):
            eta = learning_rate[network_key]
        else:
            eta = learning_rate

        # Weight decay
        if isinstance(weight_decay, dict):
            wd = weight_decay.get(network_key, 0)
        else:
            wd = weight_decay

        # Update the optimizer options
        optimizer_options_ = dict((k, v) for k, v in optimizer_options.items())
        if network_key in model_optimizer_options.keys():
            optimizer_options_.update(**model_optimizer_options)

        # Creat the optimizer
        optimizer = op(params, lr=eta, weight_decay=wd, **optimizer_options_)
        OPTIMIZERS[network_key] = optimizer

        logger.info(
            'Training {} routine with {}'.format(
                network_key, optimizer))

        # Additional regularization
        if network_key in reg.CLIPPING.keys():
            logger.info(
                'Clipping {} with {}'.format(
                    network_key,
                    reg.CLIPPING[network_key]))

        if network_key in reg.L1_DECAY.keys():
            logger.info(
                'L1 Decay {} with {}'.format(
                    network_key,
                    reg.L1_DECAY[network_key]))

    if not exp.DEVICE == torch.device('cpu'):
        cudnn.benchmark = True

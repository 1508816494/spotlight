"""
Models for recommending items given a sequece of previous items
a user has interacted with.
"""

import numpy as np

import torch

import torch.optim as optim

from torch.autograd import Variable

from spotlight.helpers import _repr_model
from spotlight.losses import (adaptive_hinge_loss,
                              bpr_loss,
                              hinge_loss,
                              pointwise_loss)
from spotlight.sequence.representations import PADDING_IDX, CNNNet, LSTMNet, PoolNet
from spotlight.sampling import sample_items
from spotlight.torch_utils import cpu, gpu, minibatch, set_seed, shuffle


class ImplicitSequenceModel(object):
    """
    Model for sequential recommendations using implicit feedback.

    Parameters
    ----------

    loss: string, optional
        The loss function for approximating a softmax with negative sampling.
        One of 'pointwise', 'bpr', 'hinge', 'adaptive_hinge', corresponding
        to losses from :class:`spotlight.losses`.
    representation: string or instance of :class:`spotlight.sequence.representations`, optional
        Sequence representation to use. If string, it must be one
        of 'pooling', 'cnn', 'lstm'; otherwise must be one of the
        representations from :class:`spotlight.sequence.representations`
    embedding_dim: int, optional
        Number of embedding dimensions to use for representing items.
        Overriden if representation is an instance of a representation class.
    n_iter: int, optional
        Number of iterations to run.
    batch_size: int, optional
        Minibatch size.
    l2: float, optional
        L2 loss penalty.
    learning_rate: float, optional
        Initial learning rate.
    optimizer: instance of a PyTorch optimizer, optional
        Overrides l2 and learning rate if supplied.
    use_cuda: boolean, optional
        Run the model on a GPU.
    sparse: boolean, optional
        Use sparse gradients for embedding layers.
    random_state: instance of numpy.random.RandomState, optional
        Random state to use when fitting.
    """

    def __init__(self,
                 loss='pointwise',
                 representation='pooling',
                 embedding_dim=32,
                 n_iter=10,
                 batch_size=256,
                 l2=0.0,
                 learning_rate=1e-2,
                 optimizer=None,
                 use_cuda=False,
                 sparse=False,
                 random_state=None):

        assert loss in ('pointwise',
                        'bpr',
                        'hinge',
                        'adaptive_hinge')

        if isinstance(representation, str):
            assert representation in ('pooling',
                                      'cnn',
                                      'lstm')

        self._loss = loss
        self._representation = representation
        self._embedding_dim = embedding_dim
        self._n_iter = n_iter
        self._learning_rate = learning_rate
        self._batch_size = batch_size
        self._l2 = l2
        self._use_cuda = use_cuda
        self._sparse = sparse
        self._optimizer = None
        self._random_state = random_state or np.random.RandomState()

        self._num_items = None
        self._net = None

        set_seed(self._random_state.randint(-10**8, 10**8),
                 cuda=self._use_cuda)

    def __repr__(self):

        return _repr_model(self)

    def fit(self, interactions, verbose=False):
        """
        Fit the model.

        Parameters
        ----------

        interactions: :class:`spotlight.interactions.SequenceInteractions`
            The input sequence dataset.
        """

        sequences = interactions.sequences.astype(np.int64)

        self._num_items = interactions.num_items

        if self._representation == 'pooling':
            self._net = PoolNet(self._num_items,
                                self._embedding_dim,
                                sparse=self._sparse)
        elif self._representation == 'cnn':
            self._net = CNNNet(self._num_items,
                               self._embedding_dim,
                               sparse=self._sparse)
        elif self._representation == 'lstm':
            self._net = LSTMNet(self._num_items,
                                self._embedding_dim,
                                sparse=self._sparse)
        else:
            self._net = self._representation

        self._net = gpu(self._net, self._use_cuda)

        if self._optimizer is None:
            self._optimizer = optim.Adam(
                self._net.parameters(),
                weight_decay=self._l2,
                lr=self._learning_rate
            )

        if self._loss == 'pointwise':
            loss_fnc = pointwise_loss
        elif self._loss == 'bpr':
            loss_fnc = bpr_loss
        elif self._loss == 'hinge':
            loss_fnc = hinge_loss
        else:
            loss_fnc = adaptive_hinge_loss

        for epoch_num in range(self._n_iter):

            sequences = shuffle(sequences,
                                random_state=self._random_state)

            sequences_tensor = gpu(torch.from_numpy(sequences),
                                   self._use_cuda)

            epoch_loss = 0.0

            for minibatch_num, batch_sequence in enumerate(minibatch(sequences_tensor,
                                                                     batch_size=self._batch_size)):

                sequence_var = Variable(batch_sequence)

                user_representation, _ = self._net.user_representation(
                    sequence_var
                )

                positive_prediction = self._net(user_representation,
                                                sequence_var)

                if self._loss == 'adaptive_hinge':
                    negative_prediction = [self._get_negative_prediction(sequence_var.size(),
                                                                         user_representation)
                                           for _ in range(5)]
                else:
                    negative_prediction = self._get_negative_prediction(sequence_var.size(),
                                                                        user_representation)

                self._optimizer.zero_grad()

                loss = loss_fnc(positive_prediction,
                                negative_prediction,
                                mask=(sequence_var != PADDING_IDX))
                epoch_loss += loss.data[0]

                loss.backward()
                self._optimizer.step()

            epoch_loss /= minibatch_num + 1

            if verbose:
                print('Epoch {}: loss {}'.format(epoch_num, epoch_loss))

    def _get_negative_prediction(self, shape, user_representation):

        negative_items = sample_items(
            self._num_items,
            shape,
            random_state=self._random_state)
        negative_var = Variable(
            gpu(torch.from_numpy(negative_items), self._use_cuda)
        )
        negative_prediction = self._net(user_representation, negative_var)

        return negative_prediction

    def predict(self, sequences, item_ids=None):
        """
        Make predictions: given a sequence of interactions, predict
        the next item in the sequence.

        Parameters
        ----------

        sequences: array, (1 x max_sequence_length)
            Array containing the indices of the items in the sequence.
        item_ids: array (num_items x 1), optional
            Array containing the item ids for which prediction scores
            are desired. If not supplied, predictions for all items
            will be computed.

        Returns
        -------

        predictions: array
            Predicted scores for all items in item_ids.
        """

        self._net.train(False)

        sequences = np.atleast_2d(sequences)

        if item_ids is None:
            item_ids = np.arange(self._num_items).reshape(-1, 1)

        sequences = torch.from_numpy(sequences.astype(np.int64).reshape(1, -1))
        item_ids = torch.from_numpy(item_ids.astype(np.int64))

        sequence_var = Variable(gpu(sequences, self._use_cuda))
        item_var = Variable(gpu(item_ids, self._use_cuda))

        _, sequence_representations = self._net.user_representation(sequence_var)
        out = self._net(sequence_representations.repeat(len(item_var), 1),
                        item_var)

        return cpu(out.data).numpy().flatten()

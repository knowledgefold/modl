import numpy as np
import scipy
from modl.dict_fact_fast import batch_weight
from numpy import linalg
from sklearn.utils import check_array
from sklearn.utils import check_random_state, gen_batches

from sklearn.linear_model import cd_fast

from modl._utils.enet_proj_fast import enet_norm_fast, enet_projection_fast

from modl._utils.randomkit.random_fast import Sampler

from modl._utils.enet_proj import enet_scale

from concurrent.futures import ThreadPoolExecutor

from math import log, ceil

from .dict_fact_fast import enet_regression_multi_gram, \
    enet_regression_single_gram


class DictFact:
    def __init__(self,
                 reduction=1,
                 learning_rate=1,
                 sample_learning_rate=0.76,
                 Dx_agg='masked',
                 G_agg='masked',
                 code_alpha=1,
                 code_l1_ratio=1,
                 n_epochs=1,
                 n_components=10,
                 batch_size=10,
                 code_pos=False,
                 comp_pos=False,
                 random_state=None,
                 comp_l1_ratio=0,
                 verbose=0,
                 callback=None,
                 n_threads=1):
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.sample_learning_rate = sample_learning_rate
        self.Dx_agg = Dx_agg
        self.G_agg = G_agg
        self.code_l1_ratio = code_l1_ratio
        self.code_alpha = code_alpha
        self.reduction = reduction
        self.comp_l1_ratio = comp_l1_ratio

        self.comp_pos = comp_pos
        self.code_pos = code_pos

        self.n_components = n_components
        self.n_epochs = n_epochs

        self.random_state = random_state

        self.verbose = verbose
        self.callback = callback

        self.n_threads = n_threads

    def fit(self, X):
        """
        Compute the matrix factorisation X ~ components_ x code_,solving for
        D, code_ = argmin_{r2 ||D^j ||_1 + (1 - r2) || D^j ||_2^2 < 1}
        1 / 2 || X - D A ||_2 + (1 - r) || A ||_2 / 2 + r || A ||_1
        Parameters
        ----------
        X:  n_samples * n_features

        Returns
        -------

        """
        X = check_array(X, order='C', dtype=[np.float32, np.float64])
        self.prepare(X=X)
        # Main loop
        n_features, n_samples = X.shape
        for _ in range(self.n_epochs):
            permutation = self.random_state_.permutation(n_features)
            X = X[permutation]
            if self.G_agg == 'average':
                self.G_average = self.G_average[permutation]
            if self.Dx_agg == 'average':
                self.Dx_average = self.Dx_average[permutation]
                self.code_ = self.code_[permutation]
            self.sample_n_iter_ = self.sample_n_iter_[permutation]

            self.partial_fit(X)
        return self

    def partial_fit(self, X, sample_indices=None):
        """
        Update the factorization using rows from X
        Parameters
        ----------
        X: ndarray, shape (n_samples, n_features)
            Input data
        sample_indices:
            Indices for each row of X. If None, consider that row i index is i
            (useful when providing the whole data to the function)
        Returns
        -------

        """
        X = check_array(X, dtype=[np.float32, np.float64], order='C')

        n_samples, n_features = X.shape
        batches = gen_batches(n_samples, self.batch_size)
        for batch in batches:
            this_X = X[batch]
            if sample_indices is None:
                these_sample_indices = batch
            else:
                these_sample_indices = sample_indices[batch]
            self._single_batch_fit(this_X, these_sample_indices)
        return self

    def prepare(self, n_samples=None, n_features=None,
                dtype=np.float64, X=None):
        if X is not None:
            X = check_array(X, order='C', dtype=[np.float32, np.float64])
            dtype = X.dtype
            # Transpose to fit usual column streaming
            this_n_samples = X.shape[0]
            if n_samples is None:
                n_samples = this_n_samples
            if n_features is None:
                n_features = X.shape[1]
            else:
                if n_features != X.shape[1]:
                    raise ValueError('n_features and X does not match')
        else:
            if n_features is None or n_samples is None:
                raise ValueError('Either provide'
                                 'shape or data to function prepare.')

        # Regression statistics
        if self.G_agg == 'average':
            self.G_average_ = np.zeros((n_samples, self.n_components,
                                        self.n_components), dtype=dtype)
        if self.Dx_agg == 'average':
            self.Dx_average_ = np.zeros((n_samples, self.n_components), dtype=dtype)
        # Dictionary statistics
        self.C_ = np.zeros((self.n_components, self.n_components), dtype=dtype)
        self.B_ = np.zeros((self.n_components, n_features), dtype=dtype)
        self.gradient_ = np.zeros((self.n_components, n_features), dtype=dtype,
                                  order='F')

        self.random_state_ = check_random_state(self.random_state)
        if X is None:
            self.components_ = self.random_state_.randn(self.n_components,
                                                        n_features)
        else:
            random_idx = self.random_state_.permutation(this_n_samples)[
                         :self.n_components]
            self.components_ = X[random_idx].copy()
        if self.comp_pos:
            self.components_[self.components_ <= 0] = \
                - self.components_[self.components_ <= 0]
        self.components_ = enet_scale(self.components_,
                                      l1_ratio=self.comp_l1_ratio,
                                      radius=1).astype(dtype)

        self.code_ = np.ones((n_samples, self.n_components), dtype=dtype)

        self.comp_norm_ = np.zeros(self.n_components, dtype=dtype)

        if self.G_agg == 'full':
            self.G_ = self.components_.dot(self.components_.T)

        self.n_iter_ = 0
        self.sample_n_iter_ = np.zeros(n_samples, dtype='int')
        self.random_state_ = check_random_state(self.random_state)
        random_seed = self.random_state_.randint(np.iinfo(np.uint32).max)
        self.feature_sampler_ = Sampler(n_features, True, True, random_seed)
        if self.verbose:
            log_lim = log(n_samples * self.n_epochs / self.batch_size, 10)
            self.verbose_iter_ = (np.logspace(0, log_lim, self.verbose,
                                              base=10) - 1) * self.batch_size
            self.verbose_iter_ = self.verbose_iter_.tolist()
        if self.n_threads > 1:
            self.pool_ = ThreadPoolExecutor(self.n_threads)

    def transform(self, X):
        dtype = self.components_.dtype
        X = check_array(X, order='C', dtype=dtype.type)
        n_samples, n_features = X.shape
        if self.G_agg != 'full':
            G = self.components_.dot(self.components_.T)
        else:
            G = self.G_
        Dx = X.dot(self.components_.T)
        code = np.ones((n_samples, self.n_components), dtype=dtype)
        if self.n_threads > 1:
            size_job = ceil(n_samples / self.n_threads)
            batches = list(gen_batches(n_samples, size_job))
            par_func = lambda batch: enet_regression_single_gram(
                G, Dx[batch], X[batch], code[batch],
                self.code_l1_ratio, self.code_alpha, self.code_pos)
            codes = self.pool_.map(par_func, batches)
            for batch, this_code in zip(batches, codes):
                code[batch] = this_code
        else:
            code = enet_regression_single_gram(
                G, Dx, X, code, self.code_l1_ratio, self.code_alpha,
                self.code_pos)
        return code

    def score(self, X):
        code = self.transform(X)
        loss = np.sum((X - code.dot(self.components_)) ** 2) / 2
        norm1_code = np.sum(np.abs(code))
        norm2_code = np.sum(code ** 2)
        regul = self.code_alpha * (norm1_code * self.code_l1_ratio
                                   + (1 - self.code_l1_ratio) * norm2_code / 2)
        return (loss + regul) / X.shape[0]

    def _single_batch_fit(self, X, sample_indices):
        if self.verbose_iter_ and self.n_iter_ >= self.verbose_iter_[0]:
            print('Iteration %i' % self.n_iter_)
            if self.callback is not None:
                self.callback(self)
            self.verbose_iter_ = self.verbose_iter_[1:]

        subset = self.feature_sampler_.yield_subset(self.reduction)
        batch_size = X.shape[0]

        self.n_iter_ += batch_size
        self.sample_n_iter_[sample_indices] += 1
        this_sample_n_iter = self.sample_n_iter_[sample_indices]
        w_sample = np.power(this_sample_n_iter, -self.sample_learning_rate)
        w = batch_weight(self.n_iter_, batch_size,
                         self.learning_rate, 0)

        self._compute_code(X, sample_indices, w_sample, subset)

        this_code = self.code_[sample_indices]

        if self.n_threads == 1:
            self._update_stat_and_dict(subset, X, this_code, w)
        else:
            self._update_stat_and_dict_parallel(subset, X,
                                                this_code, w)

    def _update_stat_and_dict(self, subset, X, code, w):
        self._update_C(code, w)
        self._update_B(X, code, w)
        self.gradient_[:, subset] = self.B_[:, subset]
        self._update_dict(subset)

    def _update_stat_and_dict_parallel(self, subset, X, this_code, w):
        self.gradient_[:, subset] = self.B_[:, subset]
        dict_thread = self.pool_.submit(self._update_stat_partial_and_dict,
                                        subset, X, this_code, w)
        B_thread = self.pool_.submit(self._update_B, X,
                                     this_code, w)
        dict_thread.result()
        B_thread.result()

    def _update_stat_partial_and_dict(self, subset, X, code, w):
        self._update_C(code, w)
        # Gradient update
        batch_size = X.shape[0]
        X_subset = X[:, subset]
        self.gradient_[:, subset] *= 1 - w
        self.gradient_[:, subset] += w * code.T.dot(X_subset) / batch_size

        self._update_dict(subset)

    def _update_B(self, X, code, w):
        batch_size = X.shape[0]
        self.B_ *= 1 - w
        self.B_ += w * code.T.dot(X) / batch_size

    def _update_C(self, this_code, w):
        batch_size = this_code.shape[0]
        self.C_ *= 1 - w
        self.C_ += w * this_code.T.dot(this_code) / batch_size

    def _compute_code(self, X, sample_indices,
                      w_sample, subset):
        batch_size, n_features = X.shape
        reduction = self.reduction

        if self.Dx_agg != 'full' or self.G_agg != 'full':
            components_subset = self.components_[:, subset]

        if self.Dx_agg == 'full':
            Dx = X.dot(self.components_.T)
        else:
            X_subset = X[:, subset]
            Dx = X_subset.dot(components_subset.T) * reduction
            if self.Dx_agg == 'average':
                self.Dx_average_[sample_indices] \
                    *= 1 - w_sample[:, np.newaxis]
                self.Dx_average_[sample_indices] \
                    += Dx * w_sample[:, np.newaxis]
                Dx = self.Dx_average_[sample_indices]

        if self.G_agg != 'full':
            G = components_subset.dot(components_subset.T) * reduction
            if self.G_agg == 'average':
                if self.n_threads > 1:
                    size_job = ceil(batch_size / self.n_threads)
                    batches = list(gen_batches(batch_size, size_job))
                    def par_func(batch):
                        self.G_average_[sample_indices[batch]] *= \
                            1 - w_sample[batch, np.newaxis, np.newaxis]

                        self.G_average_[sample_indices[batch]] += \
                            w_sample[batch, np.newaxis].dot( G[:, np.newaxis, :])
                    _ = list(self.pool_.map(par_func, batches))
                else:
                    self.G_average_[sample_indices] *= \
                        1 - w_sample[:, np.newaxis, np.newaxis]
                    # For some reason dot with 3d array makes
                    # the product along the 2nd
                    #  dimension of right side factor
                    self.G_average_[sample_indices] += \
                        w_sample[:, np.newaxis].dot(G[:, np.newaxis, :])
            G = self.G_average_[sample_indices]
        else:
            G = self.G_

        code = self.code_[sample_indices]

        if self.n_threads > 1:
            size_job = ceil(batch_size / self.n_threads)
            batches = list(gen_batches(batch_size, size_job))
            if self.G_agg == 'average':
                par_func = lambda batch: enet_regression_multi_gram(
                    G[batch], Dx[batch], X[batch], code[batch],
                    self.code_l1_ratio, self.code_alpha, self.code_pos)
            else:
                par_func = lambda batch: enet_regression_single_gram(
                    G, Dx[batch], X[batch], code[batch],
                    self.code_l1_ratio, self.code_alpha, self.code_pos)
            codes = self.pool_.map(par_func, batches)
            for batch, this_code in zip(batches, codes):
                self.code_[sample_indices[batch]] = this_code
        else:
            if self.G_agg == 'average':
                self.code_[sample_indices] = enet_regression_multi_gram(
                    G, Dx, X, code,
                    self.code_l1_ratio, self.code_alpha, self.code_pos)
            else:
                self.code_[sample_indices] = enet_regression_single_gram(
                    G, Dx, X, code,
                    self.code_l1_ratio, self.code_alpha, self.code_pos)

    def _update_dict(self, subset):
        len_subset = subset.shape[0]
        n_components, n_features = self.components_.shape
        components_subset = self.components_[:, subset]
        atom_temp = np.zeros(len_subset, dtype=self.components_.dtype)
        gradient_subset = self.gradient_[:, subset]
        ger, = scipy.linalg.get_blas_funcs(('ger',), (self.C_,
                                                      components_subset))

        if self.G_agg == 'full' and len_subset < n_features / 2.:
            self.G_ -= components_subset.dot(components_subset.T)

        gradient_subset -= self.C_.dot(components_subset)

        order = self.random_state_.permutation(n_components)
        for k in order:
            subset_norm = enet_norm_fast(components_subset[k],
                                         self.comp_l1_ratio)
            self.comp_norm_[k] += subset_norm
            gradient_subset = ger(1.0, self.C_[k], components_subset[k],
                                  a=gradient_subset, overwrite_a=True)
            if self.C_[k, k] > 1e-20:
                components_subset[k] = gradient_subset[k] / self.C_[k, k]
            # Else do not update
            if self.comp_pos:
                components_subset[components_subset < 0] = 0
            enet_projection_fast(components_subset[k],
                                 atom_temp,
                                 self.comp_norm_[k], self.comp_l1_ratio)
            components_subset[k] = atom_temp
            subset_norm = enet_norm_fast(components_subset[k],
                                         self.comp_l1_ratio)
            self.comp_norm_[k] -= subset_norm
            gradient_subset = ger(-1.0, self.C_[k], components_subset[k],
                                  a=gradient_subset, overwrite_a=True)

        self.components_[:, subset] = components_subset

        if self.G_agg == 'full':
            if len_subset < n_features / 2.:
                self.G_ += components_subset.dot(components_subset.T)
            else:
                self.G_[:] = self.components_.dot(self.components_.T)


def enet_regression_multi_gram_(G, Dx, X, code, l1_ratio, alpha,
                               positive):
    batch_size = code.shape[0]
    if l1_ratio == 0:
        n_components = G.shape[1]
        for i in range(batch_size):
            G.flat[::n_components + 1] += alpha
            code[i] = linalg.solve(G[i], Dx[i])
            G.flat[::n_components + 1] -= alpha
    else:
        # Unused but unfortunate API
        random_state = check_random_state(0)
        for i in range(batch_size):
            cd_fast.enet_coordinate_descent_gram(
                code[i],
                alpha * l1_ratio,
                alpha * (
                    1 - l1_ratio),
                G[i], Dx[i], X[i], 100, 1e-2,
                random_state,
                False, positive)
    return code


def enet_regression_single_gram_(G, Dx, X, code, code_l1_ratio, code_alpha,
                                code_pos):
    batch_size = code.shape[0]
    if code_l1_ratio == 0:
        n_components = G.shape[0]
        G = G.copy()
        G.flat[::n_components + 1] += code_alpha
        code[:] = linalg.solve(G, Dx.T).T
        G.flat[::n_components + 1] -= code_alpha
    else:
        # Unused but unfortunate API
        random_state = check_random_state(0)
        for i in range(batch_size):
            cd_fast.enet_coordinate_descent_gram(
                code[i],
                code_alpha * code_l1_ratio,
                code_alpha * (
                    1 - code_l1_ratio),
                G, Dx[i], X[i], 100, 1e-2,
                random_state,
                False, code_pos)
    return code

import numpy as np
from optimizer import OptimizerHyperparams
from ops import zeros, get_nl, softmax, mult,\
        get_nl_grad, as_np, array, log, yl_init,\
        USE_GPU, gnp, empty, tile, rand, copy_arr
from models import Net
from log_utils import get_logger
from param_utils import ModelHyperparams
from utt_char_stream import UttCharStream
from opt_utils import create_optimizer
from dset_utils import one_hot_lists

logger = get_logger()

# TODO Make hyperparameters more flexible so can specify layer info in
# more detail (multiple recurrent layers, etc)

# Large portions based off of https://github.com/awni/rnn/blob/master/rnn.py

class BRNNHyperparams(ModelHyperparams):

    def __init__(self, **entries):
        self.defaults = [
            ('hidden_size', 1000, 'size of hidden layers'),
            ('hidden_layers', 5, 'number of hidden layers'),
            ('recurrent_layer', 3, 'layer which should have recurrent connections'),
            ('bidirectional', False, 'bidirectional recurrent layers or not'),
            ('input_size', 34, 'dimension of input example'),
            ('output_size', 34, 'size of softmax output'),
            ('batch_size', 128, 'size of dataset batches'),
            ('max_act', 5.0, 'threshold to clip activation'),
            ('nl', 'relu', 'type of nonlinearity')
        ]
        super(BRNNHyperparams, self).__init__(entries)


class Layer:

    def __init__(self, inp_size, out_size, f_recur=False, b_recur=False,
            softmax=False):
        self.f_recur = f_recur
        self.b_recur = b_recur
        self.softmax = softmax

        self.W = yl_init((out_size, inp_size))
        self.b = zeros((out_size, 1))
        self.dW = zeros(self.W.shape)
        self.db = zeros(self.b.shape)

        self.params = {'W': self.W, 'b': self.b}
        self.grads = {'W': self.dW, 'b': self.db}
        if f_recur:
            self.Wf = yl_init((out_size, out_size))
            self.dWf = zeros(self.Wf.shape)
            self.params['Wf'] = self.Wf
            self.grads['Wf'] = self.dWf
            self.f_acts = None
        if b_recur:
            self.Wb = yl_init((out_size, out_size))
            self.dWb = zeros(self.Wb.shape)
            self.params['Wb'] = self.Wb
            self.grads['Wb'] = self.dWb
            self.b_acts = None


class BRNN(Net):

    def __init__(self, dset, hps, opt_hps, train=True, opt='nag'):

        super(BRNN, self).__init__(dset, hps, train=train)
        self.nl = get_nl(hps.nl)

        self.layer_specs = list()
        for k in xrange(hps.hidden_layers):
            layer_spec = dict()
            layer_spec['f_recur'] = False
            layer_spec['b_recur'] = False
            layer_spec['softmax'] = False
            if k == hps.recurrent_layer - 1:
                layer_spec['f_recur'] = True
                if hps.bidirectional:
                    layer_spec['b_recur'] = True
            self.layer_specs.append(layer_spec)

        self.alloc_params()

        if train:
            self.alloc_grads()
            self.opt = create_optimizer(opt, self, **(opt_hps.to_dict()))

    @staticmethod
    def init_hyperparams():
        return BRNNHyperparams()

    def alloc_params(self):
        hps = self.hps

        self.layers = list()
        inp_size = hps.input_size
        for k in xrange(0, hps.hidden_layers):
            layer = Layer(inp_size, hps.hidden_size, **self.layer_specs[k])
            self.params['W%d' % k] = layer.params['W']
            self.params['b%d' % k] = layer.params['b']
            if layer.f_recur:
                self.params['W%df' % k] = layer.params['Wf']
            if layer.b_recur:
                self.params['W%db' % k] = layer.params['Wb']
            inp_size = hps.hidden_size
            self.layers.append(layer)

        layer = Layer(hps.hidden_size, hps.output_size, f_recur=False,
                b_recur=False, softmax=True)
        self.params['W%d' % hps.hidden_layers] = layer.params['W']
        self.params['b%d' % hps.hidden_layers] = layer.params['b']
        self.layers.append(layer)

        self.count_params()

    def alloc_grads(self):
        # Call after allocating parameters
        self.grads = {}
        for k in xrange(len(self.layers)):
            layer = self.layers[k]
            self.grads['W%d' % k] = layer.grads['W']
            self.grads['b%d' % k] = layer.grads['b']
            if layer.f_recur:
                self.grads['W%df' % k] = layer.grads['Wf']
            if layer.b_recur:
                self.grads['W%db' % k] = layer.grads['Wb']
        grad_count = 0
        for g in self.grads:
            grad_count += reduce(lambda x, y: x*y, self.grads[g].shape)
        logger.info('Allocated %d gradients' % grad_count)

    def run(self, back=True, check_grad=False):
        if USE_GPU:
            gnp.free_reuse_cache()
        super(BRNN, self).run(back=back)

        data, labels = self.dset.get_batch()
        # FIXME Ugly
        data = one_hot_lists(data, self.hps.output_size)
        # Sometimes get less data
        self.T = data.shape[1]
        self.bsize = data.shape[2]
        # Combine time and batch indices
        data = data.reshape((data.shape[0], -1))

        if check_grad:
            cost, grads = self.cost_and_grad(data, labels)
            to_check = 'W%db' % 2
            self.check_grad(data, labels, grads, params_to_check=[to_check], eps=0.1)
        else:
            if back:
                self.update_params(data, labels)
            else:
                cost, probs = self.cost_and_grad(data, labels, back=False)
                return cost, probs

    def cost_and_grad(self, data, labels, back=True):
        # Forward prop

        self.acts = [array(data)]
        probs = self.forward_prop()

        if labels is None:
            return None, probs

        # Compute cost and grads, replace this with other cost for
        # applications that don't use softmax classification

        costs, deltas = self.cross_ent(probs, labels)

        cost = costs.sum() / self.bsize
        if not back:
            return cost, probs

        # Backprop

        self.backprop(deltas)

        # NOTE Dividing by T to get better sense if objective # is decreasing,
        # remove for grad checking
        return cost / float(self.T), self.grads

    def forward_prop(self):
        for layer in self.layers:
            out = mult(layer.W, self.acts[-1]) + layer.b

            if len(self.acts) == self.hps.hidden_layers + 1:
                break

            if layer.f_recur:
                f_acts = self.fprop_recur(layer, out)
            if layer.b_recur:
                b_acts = self.fprop_recur(layer, out, reverse=True)

            if layer.f_recur and layer.b_recur:
                out = f_acts + b_acts
            elif layer.f_recur:
                out = f_acts
            elif layer.b_recur:
                out = b_acts
            elif not layer.softmax:
                out = self.nl(out)

            self.acts.append(out)

        out = softmax(out)
        return out

    def backprop(self, deltas):
        for k in reversed(xrange(len(self.layers))):
            layer = self.layers[k]
            layer.dW[:] = mult(deltas, self.acts[k].T) / self.bsize
            layer.db[:] = deltas.sum(axis=-1).reshape((-1, 1)) / self.bsize
            if k > 0:
                deltas = mult(layer.W.T, deltas)
                layer = self.layers[k - 1]
                if layer.f_recur:
                    deltas_f = self.bprop_recur(layer, deltas)
                if layer.b_recur:
                    deltas_b = self.bprop_recur(layer, deltas, reverse=True)

                if layer.f_recur and layer.b_recur:
                    deltas = deltas_f + deltas_b
                elif layer.f_recur:
                    deltas = deltas_f
                elif layer.b_recur:
                    deltas = deltas_b
                else:
                    deltas = deltas * get_nl_grad(self.hps.nl, self.acts[k])

    def fprop_recur(self, layer, acts, reverse=False):
        acts = copy_arr(acts)
        if reverse:
            W = layer.Wb
            layer.b_acts = acts
            r_acts = layer.b_acts
        else:
            W = layer.Wf
            layer.f_acts = acts
            r_acts = layer.f_acts

        for t in xrange(self.T):
            if reverse:
                start = (self.T - t - 1) * self.bsize
            else:
                start = t * self.bsize
            r_act = acts[:, start:start+self.bsize]
            if t > 0:
                s = start + self.bsize if reverse else start - self.bsize
                r_act += mult(W, r_acts[:, s:s+self.bsize])
            r_acts[:, start:start+self.bsize] = self.nl(r_act)

        return r_acts

    def bprop_recur(self, layer, deltas, reverse=False):
        deltas = copy_arr(deltas)
        if reverse:
            start = 0
            acts = layer.b_acts
            W = layer.Wb
            dW = layer.dWb
        else:
            start = (self.T - 1) * self.bsize
            acts = layer.f_acts
            W = layer.Wf
            dW = layer.dWf

        curr_act = acts[:, start:start+self.bsize]
        curr_dt = deltas[:, start:start+self.bsize] * get_nl_grad(self.hps.nl, curr_act)

        for t in xrange(1, self.T):
            if reverse:
                start = (t + 1) * self.bsize
            else:
                start = (self.T - t) * self.bsize

            next_dt = deltas[:, start-self.bsize:start]
            next_dt += mult(W.T, curr_dt)

            curr_act = acts[:, start-self.bsize:start]
            next_dt *= get_nl_grad(self.hps.nl, curr_act)
            curr_dt = next_dt

        if reverse:
            dW[:] = mult(deltas[:, :-self.bsize], acts[:, self.bsize:].T) / self.bsize
        else:
            dW[:] = mult(deltas[:, self.bsize:], acts[:, :-self.bsize].T) / self.bsize

        return deltas

    def cross_ent(self, probs, labels):
        probs_neg_log = as_np(-1 * log(probs))
        deltas = as_np(probs)
        costs = np.zeros((self.T, self.bsize))

        for k in xrange(self.bsize):
            for t in xrange(len(labels[k])):
                # NOTE Very slow if probs_neg_log not in CPU memory
                costs[t, k] = probs_neg_log[labels[k][t], t*self.bsize+k]
                deltas[labels[k][t], t*self.bsize+k] -= 1

        return costs, array(deltas)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    model_hps = BRNNHyperparams()
    model_hps.hidden_size = 10
    opt_hps = OptimizerHyperparams()
    model_hps.add_to_argparser(parser)
    opt_hps.add_to_argparser(parser)

    args = parser.parse_args()

    model_hps.set_from_args(args)
    opt_hps.set_from_args(args)

    dset = UttCharStream(args.batch_size)

    # Construct network
    model = BRNN(dset, model_hps, opt_hps)
    model.run(check_grad=True)
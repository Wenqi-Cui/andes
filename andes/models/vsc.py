from cvxopt import matrix, mul, spmatrix, div, sin, cos, log
from .dcbase import DCBase
from ..utils.math import *
from ..consts import *


class VSC(DCBase):
    """VSC model for power flow study"""

    def __init__(self, system, name):
        super().__init__(system, name)
        self._group = 'AC/DC'
        self._name = 'VSC'
        self._ac = {'bus': ['a', 'v']}
        self._params.extend(['rsh',
                             'xsh',
                             'vshmax',
                             'vshmin',
                             'Ishmax',
                             'pref0',
                             'qref0',
                             'vref0',
                             'v0',
                             'vdcref0',
                             'k0',
                             'k1',
                             'k2',
                             'droop',
                             'K',
                             'vhigh',
                             'vlow'])
        self._data.update({'rsh': 0.0025,
                           'xsh': 0.06,
                           'vshmax': 1.1,
                           'vshmin': 0.9,
                           'Ishmax': 2,
                           'bus': None,
                           'control': None,
                           'v0': 1.0,
                           'pref0': 0,
                           'qref0': 0,
                           'vref0': 1.0,
                           'vdcref0': 1.0,
                           'k0': 0,
                           'k1': 0,
                           'k2': 0,
                           'droop': False,
                           'K': 0,
                           'vhigh': 9999,
                           'vlow': 0.0,
                           })
        self._units.update({'rsh': 'omh',
                            'xsh': 'omh',
                            'vshmax': 'pu',
                            'vshmin': 'pu',
                            'Ishmax': 'pu',
                            'vref0': 'pu',
                            'pref0': 'pu',
                            'qref0': 'pu',
                            'vdcref0': 'pu',
                            'droop': 'boolean',
                            'vhigh': 'pu',
                            'vlow': 'pu',
                            })
        self._descr.update({'rsh': 'AC interface resistance',
                            'xsh': 'AC interface reactance',
                            'vshmax': 'Maximum ac interface voltage',
                            'vshmin': 'Minimum ac interface voltage',
                            'Ishmax': 'Maximum ac current',
                            'vref0': 'AC voltage setting',
                            'pref0': 'AC active power setting',
                            'qref0': 'AC reactive power setting',
                            'vdcref0': 'DC voltage setting',
                            'k0': 'Loss coefficient - constant',
                            'k1': 'Loss coefficient - linear',
                            'k2': 'Loss coefficient - quadratic',
                            'droop': 'Enable dc voltage droop control',
                            'K': 'Droop coefficient',
                            'vhigh': 'Upper voltage threshold in droop control',
                            'vlow': 'Lower voltage threshold in droop control',
                            'control': 'Control method of this VSC in PQ, PV, vQ or vV',
                            'bus': 'AC bus idx',
                            'v0': 'AC voltage initial guess for PV and PQ controlled VSC'
                            })
        self._algebs.extend(['ash', 'vsh', 'psh', 'qsh', 'pdc', 'Ish'])
        self._unamey.extend(['ash', 'vsh', 'psh', 'qsh', 'pdc', 'Ish'])
        self._fnamey.extend(['\\theta_{sh}', 'V_{sh}', 'P_{sh}', 'Q_{sh}', 'P_{dc}', 'I_{sh}'])
        self._mandatory.extend(['bus', 'control'])
        self._service.extend(['Zsh', 'Ysh', 'glim', 'ylim', 'vdcref', 'R',
                              'PQ', 'PV', 'vV', 'vQ'])
        self.calls.update({'init0': True, 'pflow': True,
                           'gcall': True, 'gycall': True,
                           'jac0': True, 'shunt': True,
                           })
        self._inst_meta()
        self.glim = []
        self.ylim = []
        self.vio = {}

    def setup(self):
        super().setup()
        self.K = mul(self.K, self.droop)
        self.R = matrix(0, (self.n, 1), 'd')
        self.Zsh = self.rsh + 1j * self.xsh
        self.Ysh = div(1, self.Zsh)

        self.PQ = zeros(self.n, 1)
        self.PV = zeros(self.n, 1)
        self.vV = zeros(self.n, 1)
        self.vQ = zeros(self.n, 1)
        for idx, cc in enumerate(self.control):
            if cc not in ['PQ', 'PV', 'vV', 'vQ']:
                raise KeyError('VSC {0} control parameter {1} is invalid.'.format(self.name[idx], cc))
            self.__dict__[cc][idx] = 1

    def init0(self, dae):
        # behind-transformer AC theta_sh and V_sh - must assign first
        dae.y[self.ash] = dae.y[self.a] + 1e-6
        dae.y[self.vsh] = mul(self.v0, 1 - self.vV) + mul(self.vref0, self.vV) + 1e-6

        Vm = polar(dae.y[self.v], dae.y[self.a] * 1j)
        Vsh = polar(dae.y[self.vsh], dae.y[self.ash] * 1j)  # Initial value for Vsh
        IshC = conj(div(Vsh - Vm, self.Zsh))
        Ssh = mul(Vsh, IshC)

        # PQ PV and V control initials on converters
        dae.y[self.psh] = mul(self.pref0, self.PQ + self.PV)
        dae.y[self.qsh] = mul(self.qref0, self.PQ)
        dae.y[self.v1] = dae.y[self.v2] + mul(dae.y[self.v1], 1 - self.vV) + mul(self.vdcref0, self.vV)

        # PV and V control on AC buses
        dae.y[self.v] = mul(dae.y[self.v], 1 - self.PV - self.vV) + mul(self.vref0, self.PV + self.vV)

        # Converter current initial
        dae.y[self.Ish] = abs(IshC)

        # Converter dc power output
        dae.y[self.pdc] = mul(Vsh, IshC).real() + \
                          (self.k0 + mul(self.k1, dae.y[self.Ish]) + mul(self.k2, mul(dae.y[self.Ish], dae.y[self.Ish])))

    def gcall(self, dae):
        if sum(self.u) == 0:
            return

        Vm = polar(dae.y[self.v], dae.y[self.a])
        Vsh = polar(dae.y[self.vsh], dae.y[self.ash])
        Ish = mul(self.Ysh, Vsh - Vm)
        IshC = conj(Ish)
        Ssh = mul(Vm, IshC)

        # check the Vsh and Ish limits during PF iterations
        vupper = list(abs(Vsh) - self.vshmax)
        vlower = list(abs(Vsh) - self.vshmin)
        iupper = list(abs(IshC) - self.Ishmax)
        # check for Vsh and Ish limit violations
        if self.system.SPF.iter >= self.system.SPF.ipv2pq:
            for i in range(self.n):
                if self.u[i] and (vupper[i] > 0 or vlower[i] < 0 or iupper[i] > 0):
                    if i not in self.vio.keys():
                        self.vio[i] = list()
                    if vupper[i] > 0:
                        if 'vmax' not in self.vio[i]:
                            self.vio[i].append('vmax')
                            self.system.Log.debug(' * Vmax reached for VSC_{0}'.format(i))
                    elif vlower[i] < 0:
                        if 'vmin' not in self.vio[i]:
                            self.vio[i].append('vmin')
                            self.system.Log.debug(' * Vmin reached for VSC_{0}'.format(i))
                    if iupper[i] > 0:
                        if 'Imax' not in self.vio[i]:
                            self.vio[i].append('Imax')
                            self.system.Log.debug(' * Imax reached for VSC_{0}'.format(i))

        # AC interfaces - power
        dae.g[self.a] -= mul(self.u, dae.y[self.psh])  # active power load
        dae.g[self.v] -= mul(self.u, dae.y[self.qsh])  # reactive power load

        # DC interfaces - current
        above = list(dae.y[self.v1] - self.vhigh)
        below = list(dae.y[self.v1] - self.vlow)
        above = matrix([1 if i > 0 else 0 for i in above])
        below = matrix([1 if i < 0 else 0 for i in below])
        self.R = mul(above or below, self.K)
        self.vdcref = mul(self.droop, above, self.vhigh) + mul(self.droop, below, self.vlow)
        dae.g[self.v1] += div(mul(self.u, dae.y[self.pdc]), dae.y[self.v1] - dae.y[self.v2])  # current injection
        dae.g -= spmatrix(div(mul(self.u, dae.y[self.pdc]), dae.y[self.v1] - dae.y[self.v2]), self.v2, [0]*self.n, (dae.m, 1), 'd')  # negative current injection

        dae.g[self.ash] = mul(self.u, Ssh.real() - dae.y[self.psh])  # (2)
        dae.g[self.vsh] = mul(self.u, Ssh.imag() - dae.y[self.qsh])  # (3)

        # PQ, PV or V control
        dae.g[self.psh] = mul(dae.y[self.psh] + mul(self.R, dae.y[self.v1] - self.vdcref) - self.pref0, self.PQ + self.PV, self.u) + mul((dae.y[self.v1] - dae.y[self.v2]) - self.vdcref0, self.vV + self.vQ, self.u)  # (12), (15)
        dae.g[self.qsh] = mul(dae.y[self.qsh] - self.qref0, self.PQ + self.vQ, self.u) + mul(dae.y[self.v] - self.vref0, self.PV + self.vV, self.u)  # (13), (16)

        for comp, var in self.vio.items():
            for count, item in enumerate(var):
                if item == 'vmax':
                    yidx = self.vsh[comp]
                    ylim = self.vshmax[comp]
                elif item == 'vmin':
                    yidx = self.vsh[comp]
                    ylim = self.vshmin[comp]
                elif item == 'Imax':
                    yidx = self.Ish[comp]
                    ylim = self.Ishmax[comp]
                else:
                    raise NameError('Unknown limit variable name <{0}>.'.format(item))

                if count == 0:
                    idx = self.qsh[comp]
                    self.switch(comp, 'Q')
                else:
                    idx = self.psh[comp]
                    self.switch(comp, 'P')

                self.system.DAE.factorize = True
                dae.g[idx] = dae.y[yidx] - ylim
                if idx not in self.glim:
                    self.glim.append(idx)
                if yidx not in self.ylim:
                    self.ylim.append(yidx)

        dae.g[self.Ish] = mul(self.u, abs(IshC) - dae.y[self.Ish])  # (10)

        dae.g[self.pdc] = mul(self.u,
                              mul(Vsh, IshC).real() - dae.y[self.pdc] + (
                              self.k0 + mul(self.k1, dae.y[self.Ish]) + mul(self.k2, dae.y[self.Ish] ** 2))
                              )

    def switch(self, idx, control):
        """Switch a single control of <idx>"""
        old = None
        new = None
        if control == 'Q':
            if self.PQ[idx] == 1:
                old = 'PQ'
                new = 'PV'
            elif self.vQ[idx] == 1:
                old = 'vQ'
                new = 'vV'
        elif control == 'P':
            if self.PQ[idx] == 1:
                old = 'PQ'
                new = 'vQ'
            elif self.PV[idx] == 1:
                old = 'PV'
                new = 'vV'
        elif control == 'V':
            if self.PV[idx] == 1:
                old = 'PV'
                new = 'PQ'
            elif self.vV[idx] == 1:
                old = 'vV'
                new = 'vQ'
        elif control == 'v':
            if self.vQ[idx] == 1:
                old = 'vQ'
                new = 'PQ'
            elif self.vV[idx] == 1:
                old = 'vV'
                new = 'PV'
        if old and new:
            self.__dict__[old][idx] = 0
            self.__dict__[new][idx] = 1

    def gycall(self, dae):
        if sum(self.u) == 0:
            return
        Zsh = self.rsh + 1j * self.xsh
        iZsh = div(self.u, abs(Zsh))
        Vh = polar(dae.y[self.v], dae.y[self.a] * 1j)
        Vsh = polar(dae.y[self.vsh], dae.y[self.ash] * 1j)
        Ish = div(Vsh - Vh + 1e-6, Zsh)
        iIsh = div(self.u, Ish)

        gsh = div(self.u, Zsh).real()
        bsh = div(self.u, Zsh).imag()
        V = dae.y[self.v]
        theta = dae.y[self.a]
        Vsh = dae.y[self.vsh]
        thetash = dae.y[self.ash]
        Vdc = dae.y[self.v1] - dae.y[self.v2]
        iVdc2 = div(self.u, Vdc ** 2)

        dae.add_jac(Gy, div(self.u, Vdc), self.v1, self.pdc)
        dae.add_jac(Gy, -mul(self.u, dae.y[self.pdc], iVdc2), self.v1, self.v1)
        dae.add_jac(Gy, mul(self.u, dae.y[self.pdc], iVdc2), self.v1, self.v2)

        dae.add_jac(Gy, -div(self.u, Vdc), self.v2, self.pdc)
        dae.add_jac(Gy, mul(self.u, dae.y[self.pdc], iVdc2), self.v2, self.v1)
        dae.add_jac(Gy, -mul(self.u, dae.y[self.pdc], iVdc2), self.v2, self.v2)

        dae.add_jac(Gy, -2*mul(gsh, V) + mul(gsh, Vsh, cos(theta - thetash)) + mul(bsh, Vsh, sin(theta - thetash)), self.ash, self.v)
        dae.add_jac(Gy, mul(gsh, V, cos(theta - thetash)) + mul(bsh, V, sin(theta - thetash)), self.ash, self.vsh)
        dae.add_jac(Gy, -mul(gsh, V, Vsh, sin(theta - thetash)) + mul(bsh, V, Vsh, cos(theta - thetash)), self.ash, self.a)
        dae.add_jac(Gy, mul(gsh, V, Vsh, sin(theta - thetash)) - mul(bsh, V, Vsh, cos(theta - thetash)), self.ash, self.ash)

        dae.add_jac(Gy, 2*mul(bsh, V) + mul(gsh, Vsh, sin(theta - thetash)) - mul(bsh, Vsh, cos(theta - thetash)), self.vsh, self.v)
        dae.add_jac(Gy, mul(gsh, V, sin(theta - thetash)) - mul(bsh, V, cos(theta - thetash)), self.vsh, self.vsh)
        dae.add_jac(Gy, mul(gsh, V, Vsh, cos(theta - thetash)) + mul(bsh, V, Vsh, sin(theta - thetash)), self.vsh, self.a)
        dae.add_jac(Gy, -mul(gsh, V, Vsh, cos(theta - thetash)) - mul(bsh, V, Vsh, sin(theta - thetash)), self.vsh, self.ash)

        dae.add_jac(Gy, 0.5 * mul(self.u, 2*V - 2*mul(Vsh, cos(theta - thetash)), abs(iIsh), abs(iZsh) ** 2), self.Ish, self.v)
        dae.add_jac(Gy, 0.5 * mul(self.u, 2*Vsh - 2*mul(V, cos(theta - thetash)), abs(iIsh), abs(iZsh) ** 2), self.Ish, self.vsh)
        dae.add_jac(Gy, 0.5 * mul(self.u, 2*V, Vsh, sin(theta-thetash), abs(iIsh), abs(iZsh) ** 2), self.Ish, self.a)
        dae.add_jac(Gy, 0.5 * mul(self.u, 2*V, Vsh, - sin(theta - thetash), abs(iIsh), abs(iZsh) ** 2), self.Ish, self.ash)

        dae.add_jac(Gy, -2 * mul(self.u, self.k2, dae.y[self.Ish]), self.pdc, self.Ish)

        dae.add_jac(Gy, mul(2 * gsh, Vsh) - mul(gsh, V, cos(theta - thetash)) + mul(bsh, V, sin(theta - thetash)), self.pdc, self.vsh)
        dae.add_jac(Gy, -mul(gsh, Vsh, cos(theta - thetash)) + mul(bsh, Vsh, sin(theta - thetash)), self.pdc, self.v)
        dae.add_jac(Gy, mul(gsh, V, Vsh, sin(theta - thetash)) + mul(bsh, V, Vsh, cos(theta - thetash)), self.pdc, self.a)
        dae.add_jac(Gy, -mul(gsh, V, Vsh, sin(theta - thetash)) - mul(bsh, V, Vsh, cos(theta - thetash)), self.pdc, self.ash)

        for gidx, yidx in zip(self.glim, self.ylim):
            dae.set_jac(Gy, 0.0, [gidx] * dae.m, range(dae.m))
            dae.set_jac(Gy, 1.0, [gidx], [yidx])
            dae.set_jac(Gy, -1e-6, [gidx], [gidx])

    def jac0(self, dae):
        dae.add_jac(Gy0, -1e-6, self.v1, self.v1)
        dae.add_jac(Gy0, -1e-6, self.v2, self.v2)
        dae.add_jac(Gy0, -1e-6, self.ash, self.ash)
        dae.add_jac(Gy0, -1e-6, self.vsh, self.vsh)

        dae.add_jac(Gy0, -self.u, self.ash, self.psh)
        dae.add_jac(Gy0, -self.u, self.vsh, self.qsh)

        dae.add_jac(Gy0, mul(self.u, self.PQ + self.PV) - 1e-6, self.psh, self.psh)
        dae.add_jac(Gy0, mul(self.u, self.vV), self.psh, self.v1)
        dae.add_jac(Gy0, -mul(self.u, self.vV), self.psh, self.v2)
        dae.add_jac(Gy0, mul(self.PV + self.PQ, self.u, self.R), self.psh, self.v1)

        dae.add_jac(Gy0, mul(self.u, self.PQ) - 1e-6, self.qsh, self.qsh)
        dae.add_jac(Gy0, mul(self.u, self.PV + self.vV), self.qsh, self.v)

        dae.add_jac(Gy0, -self.u, self.a, self.psh)
        dae.add_jac(Gy0, -self.u, self.v, self.qsh)

        dae.add_jac(Gy0, -self.u - 1e-6, self.Ish, self.Ish)

        dae.add_jac(Gy0, -self.u - 1e-6, self.pdc, self.pdc)
        dae.add_jac(Gy0, mul(self.u, self.k1), self.pdc, self.Ish)

    def disable(self, idx):
        """Disable an element and reset the outputs"""
        if idx not in self.int.keys():
            self.message('Element index {0} does not exist.'.format(idx))
            return
        self.u[self.int[idx]] = 0
        # self.system.DAE.y[self.psh] = 0
        # self.system.DAE.y[self.qsh] = 0
        # self.system.DAE.y[self.pdc] = 0
        # self.system.DAE.y[self.Ish] = 0


class VSC1_Common(DCBase):
    """Common equations for VSC1"""
    def __init__(self, system, name):
        super(VSC1_Common, self).__init__(system, name)
        self._data.update({'vsc': None})
        self._mandatory.extend(['vsc'])
        self._descr.update({'vsc': 'static vsc idx'})
        self._algebs.extend(['ref1', 'ref2', 'vd', 'vq', 'p', 'q'])
        self._fnamey.extend(['ref_1', 'ref2', 'v_d', 'v_q', 'P', 'Q'])
        self._service.extend(['ref10', 'ref20'])

        self.calls.update({'fcall': True, 'fxcall': True, 'gcall': True, 'gycall': True, 'jac0': True, 'init1': True})
        self._mandatory.remove('node1')
        self._mandatory.remove('node2')
        self._mandatory.remove('Vdcn')
        self._ac = {}
        self._dc = {}

    def servcall(self, dae):
        self.copy_param('VSC', 'rsh', 'rsh', self.vsc)
        self.copy_param('VSC', 'xsh', 'xsh', self.vsc)
        self.copy_param('VSC', 'PQ', 'PQ', self.vsc)
        self.copy_param('VSC', 'PV', 'PV', self.vsc)
        self.copy_param('VSC', 'vQ', 'vQ', self.vsc)
        self.copy_param('VSC', 'vV', 'vV', self.vsc)
        self.copy_param('VSC', 'a', 'a', self.vsc)
        self.copy_param('VSC', 'v', 'v', self.vsc)
        self.copy_param('VSC', 'v1', 'v1', self.vsc)
        self.copy_param('VSC', 'v2', 'v2', self.vsc)
        self.copy_param('VSC', 'psh', 'pref0', self.vsc)
        self.copy_param('VSC', 'qsh', 'qref0', self.vsc)

        self.pref0 = dae.y[self.pref0]
        self.qref0 = dae.y[self.qref0]
        self.ref10 = mul(self.pref0, self.PQ + self.PV) + mul(dae.y[self.v1] - dae.y[self.v2], self.vQ + self.vV)
        self.ref20 = mul(self.qref0, self.PQ + self.vQ) + mul(dae.y[self.v], self.PV + self.vV)

    def init1(self, dae):
        self.servcall(dae)

        self.pll_init1(dae)

        dae.y[self.ref1] = self.ref10
        dae.y[self.ref2] = self.ref20
        dae.y[self.vd] = mul(dae.y[self.v], cos(dae.y[self.a] - dae.y[self.adq]))
        dae.y[self.vq] = - mul(dae.y[self.v], sin(dae.y[self.a] - dae.y[self.adq]))

        self.current_init1(dae)
        self.outer_init1(dae)

        dae.y[self.p] = mul(dae.x[self.Id], dae.y[self.vd]) + mul(dae.x[self.Iq], dae.y[self.vq])
        dae.y[self.q] = mul(dae.x[self.Iq], dae.y[self.vd]) - mul(dae.x[self.Id], dae.y[self.vq])

        for idx in self.vsc:
            self.system.VSC.disable(idx)

    def gcall(self, dae):
        self.pll_gcall(dae)
        self.outer_gcall(dae)
        self.current_gcall(dae)

        dae.g[self.ref1] = self.ref10 - dae.y[self.ref1]
        dae.g[self.ref2] = self.ref20 - dae.y[self.ref2]
        dae.g[self.vd] = -dae.y[self.vd] + mul(dae.y[self.v], cos(dae.y[self.a] - dae.y[self.adq]))
        dae.g[self.vq] = -dae.y[self.vq] - mul(dae.y[self.v], sin(dae.y[self.a] - dae.y[self.adq]))
        dae.g[self.p] = -dae.y[self.p] + mul(dae.x[self.Id], dae.y[self.vd]) + mul(dae.x[self.Iq], dae.y[self.vq])
        dae.g[self.q] = -dae.y[self.q] + mul(dae.x[self.Iq], dae.y[self.vd]) - mul(dae.x[self.Id], dae.y[self.vq])
        dae.g += spmatrix(- dae.y[self.p], self.a, [0]*self.n, (dae.m, 1), 'd')
        dae.g += spmatrix(- dae.y[self.q], self.v, [0]*self.n, (dae.m, 1), 'd')

    def fcall(self, dae):
        self.pll_fcall(dae)
        self.outer_fcall(dae)
        self.current_fcall(dae)

    def gycall(self, dae):
        self.pll_gycall(dae)
        self.outer_gycall(dae)
        self.current_gycall(dae)

        dae.add_jac(Gy, cos(dae.y[self.a] - dae.y[self.adq]), self.vd, self.v)
        dae.add_jac(Gy, - mul(dae.y[self.v], sin(dae.y[self.a] - dae.y[self.adq])), self.vd, self.a)
        dae.add_jac(Gy, mul(dae.y[self.v], sin(dae.y[self.a] - dae.y[self.adq])), self.vd, self.adq)
        dae.add_jac(Gy, - sin(dae.y[self.a] - dae.y[self.adq]), self.vq, self.v)
        dae.add_jac(Gy, - mul(dae.y[self.v], cos(dae.y[self.a] - dae.y[self.adq])), self.vq, self.a)
        dae.add_jac(Gy, mul(dae.y[self.v], cos(dae.y[self.a] - dae.y[self.adq])), self.vq, self.adq)
        dae.add_jac(Gy, dae.x[self.Iq], self.p, self.vq)
        dae.add_jac(Gy, dae.x[self.Id], self.p, self.vd)
        dae.add_jac(Gy, - dae.x[self.Id], self.q, self.vq)
        dae.add_jac(Gy, dae.x[self.Iq], self.q, self.vd)

    def fxcall(self, dae):
        self.pll_fxcall(dae)
        self.outer_fxcall(dae)
        self.current_fxcall(dae)

        dae.add_jac(Gx, dae.y[self.vd], self.p, self.Id)
        dae.add_jac(Gx, dae.y[self.vq], self.p, self.Iq)
        dae.add_jac(Gx, - dae.y[self.vq], self.q, self.Id)
        dae.add_jac(Gx, dae.y[self.vd], self.q, self.Iq)

    def jac0(self, dae):
        self.pll_jac0(dae)
        self.outer_jac0(dae)
        self.current_jac0(dae)

        dae.add_jac(Gy0, -self.u - 1e-6, self.ref1, self.ref1)
        dae.add_jac(Gy0, -self.u - 1e-6, self.ref2, self.ref2)
        dae.add_jac(Gy0, -self.u - 1e-6, self.vd, self.vd)
        dae.add_jac(Gy0, -self.u - 1e-6, self.vq, self.vq)
        dae.add_jac(Gy0, -self.u - 1e-6, self.p, self.p)
        dae.add_jac(Gy0, -self.u - 1e-6, self.q, self.q)
        dae.add_jac(Gy0, -self.u , self.a, self.p)
        dae.add_jac(Gy0, -self.u, self.v, self.q)


class Current1(object):
    """Inner current controllers with two PIs"""

    def __init__(self, system, name):
        self._data.update({'Tt': 0.01,
                           'Kp1': 0.2,
                           'Ki1': 1, }
                          )
        self._params.extend(['Tt', 'Kp1', 'Ki1'])
        self._states.extend(['ud', 'uq', 'Id', 'Iq', 'Md', 'Mq'])
        self._fnamex.extend(['u_d', 'u_q', 'I_d', 'I_q', 'M_d', 'M_q'])
        self._descr.update({'Ki1': 'Innercurrent controller integrator gain',
                            'Kp1': 'Inner current controller proportional gain',
                            'Tt': 'ac voltage measurement delay time constant',
                            })
        self._service.extend(['iTt', 'iLsh'])

    def current_servcall(self, dae):
        self.iTt = div(1, self.Tt)
        self.iLsh = div(1, self.xsh)

    def current_init1(self, dae):
        self.current_servcall(dae)
        dae.x[self.Id] = mul(self.pref0, div(1, dae.y[self.vd]))
        dae.x[self.Iq] = mul(self.qref0, div(1, dae.y[self.vd]))
        dae.x[self.ud] = dae.y[self.vd] + mul(dae.x[self.Id], self.rsh) + mul(dae.x[self.Iq], self.xsh)
        dae.x[self.uq] = dae.y[self.vq] + mul(dae.x[self.Iq], self.rsh) - mul(dae.x[self.Id], self.xsh)
        dae.x[self.Md] = mul(dae.x[self.Id], self.rsh)
        dae.x[self.Mq] = mul(dae.x[self.Iq], self.rsh)

    def current_fcall(self, dae):
        dae.f[self.Id] = -dae.x[self.Iq] + mul(self.iLsh, dae.x[self.ud] - dae.y[self.vd]) - mul(dae.x[self.Id], self.iLsh, self.rsh)
        dae.f[self.Iq] = dae.x[self.Id] + mul(self.iLsh, dae.x[self.uq] - dae.y[self.vq]) - mul(dae.x[self.Iq], self.iLsh, self.rsh)
        dae.f[self.ud] = mul(self.iTt, dae.x[self.Md] + dae.y[self.vd] - dae.x[self.ud] + mul(dae.y[self.Iqref], self.xsh) + mul(self.Kp1, dae.y[self.Idref] - dae.x[self.Id]))
        dae.f[self.uq] = mul(self.iTt, dae.x[self.Mq] + dae.y[self.vq] - dae.x[self.uq] + mul(self.Kp1, dae.y[self.Iqref] - dae.x[self.Iq]) - mul(dae.y[self.Idref], self.xsh))
        dae.f[self.Md] = mul(self.Ki1, dae.y[self.Idref] - dae.x[self.Id])
        dae.f[self.Mq] = mul(self.Ki1, dae.y[self.Iqref] - dae.x[self.Iq])

    def current_jac0(self, dae):
        dae.add_jac(Fx0, -1, self.Id, self.Iq)
        dae.add_jac(Fx0, self.iLsh, self.Id, self.ud)
        dae.add_jac(Fx0, - mul(self.iLsh, self.rsh), self.Id, self.Id)
        dae.add_jac(Fx0, - mul(self.iLsh, self.rsh), self.Iq, self.Iq)
        dae.add_jac(Fx0, self.iLsh, self.Iq, self.uq)
        dae.add_jac(Fx0, 1, self.Iq, self.Id)
        dae.add_jac(Fx0, - self.iTt, self.ud, self.ud)
        dae.add_jac(Fx0, - mul(self.Kp1, self.iTt), self.ud, self.Id)
        dae.add_jac(Fx0, self.iTt, self.ud, self.Md)
        dae.add_jac(Fx0, self.iTt, self.uq, self.Mq)
        dae.add_jac(Fx0, - self.iTt, self.uq, self.uq)
        dae.add_jac(Fx0, - mul(self.Kp1, self.iTt), self.uq, self.Iq)
        dae.add_jac(Fx0, - self.Ki1, self.Md, self.Id)
        dae.add_jac(Fx0, - self.Ki1, self.Mq, self.Iq)

        dae.add_jac(Fy0, - self.iLsh, self.Id, self.vd)
        dae.add_jac(Fy0, - self.iLsh, self.Iq, self.vq)
        dae.add_jac(Fy0, mul(self.Kp1, self.iTt), self.ud, self.Idref)
        dae.add_jac(Fy0, mul(self.iTt, self.xsh), self.ud, self.Iqref)
        dae.add_jac(Fy0, self.iTt, self.ud, self.vd)
        dae.add_jac(Fy0, - mul(self.iTt, self.xsh), self.uq, self.Idref)
        dae.add_jac(Fy0, mul(self.Kp1, self.iTt), self.uq, self.Iqref)
        dae.add_jac(Fy0, self.iTt, self.uq, self.vq)
        dae.add_jac(Fy0, self.Ki1, self.Md, self.Idref)
        dae.add_jac(Fy0, self.Ki1, self.Mq, self.Iqref)

    def current_gcall(self, dae):
        pass

    def current_gycall(self, dae):
        pass

    def current_fxcall(self, dae):
        pass


class VSC1_Outer1(object):
    """Outer power control loop for VSC1"""

    def __init__(self, system, name):
        self._data.update({'Ki2': 1,
                           'Ki3': 1,
                           'Kp2': 0.2,
                           'Kp3': 0.2,
                           'Tdc': 0.01,
                           })
        self._descr.update({'Ki2': 'P or Vdc voltage controller integrator gain',
                            'Ki3': 'Q or V controller integrator gain',
                            'Kp2': 'P or Vdc controller proportional gain',
                            'Kp3': 'Q or V controller proportional gain',
                            'Tdc': 'dc voltage time constant',
                            })
        self._params.extend(['Kp2', 'Ki2', 'Kp3', 'Ki3', 'Tdc'])

        self._algebs.extend(['Idref', 'Iqref', 'Idcy'])
        self._fnamey.extend(['I_d^{ref}', 'I_q^{ref}', 'I_{dcy}'])
        self._states.extend(['Nd', 'Nq', 'Idcx'])
        self._fnamex.extend(['N_d', 'N_q', 'I_{dcx}'])
        self._service.extend(['iTdc'])

    def outer_servcall(self, dae):
        self.iTdc = div(1, self.Tdc)

    def outer_init1(self, dae):
        self.outer_servcall(dae)
        dae.y[self.Idref] = mul(self.pref0, div(1, dae.y[self.vd]))
        dae.y[self.Iqref] = mul(self.qref0, div(1, dae.y[self.vd]))
        dae.y[self.Idcy] = mul(div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV, -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq]))
        dae.x[self.Idcx] = mul(div(1, dae.y[self.v1] - dae.y[self.v2]), self.vQ + self.vV, -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq]))
        dae.x[self.Nd] = mul(dae.x[self.Idcx], self.vQ + self.vV)
        dae.x[self.Nq] = mul(dae.x[self.Iq], self.PV + self.vV)

    def outer_gcall(self, dae):
        dae.g[self.Idref] = -dae.y[self.Idref] + mul(self.PQ + self.PV, dae.x[self.Nd] + mul(self.Kp2, dae.y[self.ref1] - dae.y[self.p]) + mul(dae.y[self.ref1], div(1, dae.y[self.vd]))) + mul(div(1, dae.x[self.ud]), self.vQ + self.vV, -mul(dae.x[self.Idcx], dae.y[self.v1] - dae.y[self.v2]) - mul(dae.x[self.Iq], dae.x[self.uq]))
        dae.g[self.Iqref] = dae.x[self.Nq] - dae.y[self.Iqref] + mul(self.PQ + self.vQ, mul(self.Kp3, dae.y[self.ref2] - dae.y[self.q]) + mul(dae.y[self.ref2], div(1, dae.y[self.vd]))) + mul(self.Kp3, self.PV + self.vV, dae.y[self.ref2] - dae.y[self.vd])
        dae.g[self.Idcy] = -dae.y[self.Idcy] + mul(div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV, -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq]))
        dae.g += spmatrix(mul(dae.y[self.Idcy], -self.PQ - self.PV) - mul(dae.x[self.Idcx], self.vQ + self.vV), self.v1, [0]*self.n, (dae.m, 1), 'd')
        dae.g += spmatrix(mul(dae.x[self.Idcx], self.vQ + self.vV) + mul(dae.y[self.Idcy], self.PQ + self.PV), self.v2, [0]*self.n, (dae.m, 1), 'd')

    def outer_fcall(self, dae):
        dae.f[self.Nd] = mul(self.Ki2, self.PQ + self.PV, dae.y[self.ref1] - dae.y[self.p]) + mul(self.Ki2, dae.y[self.ref1] - dae.y[self.v1], self.vQ + self.vV)
        dae.f[self.Nq] = mul(self.Ki3, self.PQ + self.vQ, dae.y[self.ref2] - dae.y[self.q]) + mul(self.Ki3, self.PV + self.vV, dae.y[self.ref2] - dae.y[self.vd])
        dae.f[self.Idcx] = mul(self.iTdc, self.vQ + self.vV, -dae.x[self.Idcx] - dae.x[self.Nd] - mul(self.Kp2, dae.y[self.ref1] - dae.y[self.v1]))

    def outer_gycall(self, dae):
        dae.add_jac(Gy, mul(self.Kp2 + div(1, dae.y[self.vd]), self.PQ + self.PV), self.Idref, self.ref1)
        dae.add_jac(Gy, mul(dae.x[self.Idcx], div(1, dae.x[self.ud]), self.vQ + self.vV), self.Idref, self.v2)
        dae.add_jac(Gy, - mul(dae.x[self.Idcx], div(1, dae.x[self.ud]), self.vQ + self.vV), self.Idref, self.v1)
        dae.add_jac(Gy, - mul(dae.y[self.ref1], (dae.y[self.vd])**-2, self.PQ + self.PV), self.Idref, self.vd)
        dae.add_jac(Gy, mul(self.Kp3, self.PV + self.vV) + mul(self.Kp3 + div(1, dae.y[self.vd]), self.PQ + self.vQ), self.Iqref, self.ref2)
        dae.add_jac(Gy, -mul(self.Kp3, self.PV + self.vV) - mul(dae.y[self.ref2], (dae.y[self.vd])**-2, self.PQ + self.vQ), self.Iqref, self.vd)
        dae.add_jac(Gy, mul((dae.y[self.v1] - dae.y[self.v2])**-2, self.PQ + self.PV, -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.Idcy, self.v2)
        dae.add_jac(Gy, - mul((dae.y[self.v1] - dae.y[self.v2])**-2, self.PQ + self.PV, -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.Idcy, self.v1)

    def outer_fxcall(self, dae):
        dae.add_jac(Gx, mul(div(1, dae.x[self.ud]), dae.y[self.v2] - dae.y[self.v1], self.vQ + self.vV), self.Idref, self.Idcx)
        dae.add_jac(Gx, - mul((dae.x[self.ud])**-2, self.vQ + self.vV, -mul(dae.x[self.Idcx], dae.y[self.v1] - dae.y[self.v2]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.Idref, self.ud)
        dae.add_jac(Gx, - mul(dae.x[self.Iq], div(1, dae.x[self.ud]), self.vQ + self.vV), self.Idref, self.uq)
        dae.add_jac(Gx, - mul(dae.x[self.uq], div(1, dae.x[self.ud]), self.vQ + self.vV), self.Idref, self.Iq)
        dae.add_jac(Gx, - mul(dae.x[self.Id], div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV), self.Idcy, self.ud)
        dae.add_jac(Gx, - mul(dae.x[self.Iq], div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV), self.Idcy, self.uq)
        dae.add_jac(Gx, - mul(dae.x[self.ud], div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV), self.Idcy, self.Id)
        dae.add_jac(Gx, - mul(dae.x[self.uq], div(1, dae.y[self.v1] - dae.y[self.v2]), self.PQ + self.PV), self.Idcy, self.Iq)

    def outer_jac0(self, dae):
        dae.add_jac(Gy0, -1, self.Idref, self.Idref)
        dae.add_jac(Gy0, - mul(self.Kp2, self.PQ + self.PV), self.Idref, self.p)
        dae.add_jac(Gy0, - mul(self.Kp3, self.PQ + self.vQ), self.Iqref, self.q)
        dae.add_jac(Gy0, -1, self.Iqref, self.Iqref)
        dae.add_jac(Gy0, -1, self.Idcy, self.Idcy)
        dae.add_jac(Gy0, -self.PQ - self.PV, self.v1, self.Idcy)
        dae.add_jac(Gy0, self.PQ + self.PV, self.v2, self.Idcy)
        dae.add_jac(Gx0, self.PQ + self.PV, self.Idref, self.Nd)
        dae.add_jac(Gx0, 1, self.Iqref, self.Nq)
        dae.add_jac(Gx0, -self.vQ - self.vV, self.v1, self.Idcx)
        dae.add_jac(Gx0, self.vQ + self.vV, self.v2, self.Idcx)

        dae.add_jac(Fx0, - mul(self.iTdc, self.vQ + self.vV), self.Idcx, self.Idcx)
        dae.add_jac(Fx0, - mul(self.iTdc, self.vQ + self.vV), self.Idcx, self.Nd)
        dae.add_jac(Fy0, mul(self.Ki2, self.PQ + self.PV) + mul(self.Ki2, self.vQ + self.vV), self.Nd, self.ref1)
        dae.add_jac(Fy0, - mul(self.Ki2, self.vQ + self.vV), self.Nd, self.v1)
        dae.add_jac(Fy0, - mul(self.Ki2, self.PQ + self.PV), self.Nd, self.p)
        dae.add_jac(Fy0, mul(self.Ki3, self.PQ + self.vQ) + mul(self.Ki3, self.PV + self.vV), self.Nq, self.ref2)
        dae.add_jac(Fy0, - mul(self.Ki3, self.PV + self.vV), self.Nq, self.vd)
        dae.add_jac(Fy0, - mul(self.Ki3, self.PQ + self.vQ), self.Nq, self.q)
        dae.add_jac(Fy0, - mul(self.Kp2, self.iTdc, self.vQ + self.vV), self.Idcx, self.ref1)
        dae.add_jac(Fy0, mul(self.Kp2, self.iTdc, self.vQ + self.vV), self.Idcx, self.v1)
        dae.add_jac(Gy0, 1e-6, self.Idref, self.Idref)
        dae.add_jac(Gy0, 1e-6, self.Iqref, self.Iqref)
        dae.add_jac(Gy0, 1e-6, self.Idcy, self.Idcy)


class PLL1(object):
    """Ideal tracking PLL"""
    def __init__(self, system, name):
        self._algebs.extend(['adq'])
        self._params.extend(['adq'])
        self._fnamey.extend(['\\theta{dq}'])

    def pll_init1(self, dae):
        dae.y[self.adq] = dae.y[self.a]

    def pll_gcall(self, dae):
        dae.g[self.adq] = dae.y[self.adq] - dae.y[self.a]

    def pll_jac0(self, dae):
        dae.add_jac(Gy0, -1, self.adq, self.a)
        dae.add_jac(Gy0, 1 + 1e-6, self.adq, self.adq)

    def pll_fcall(self, dae):
        pass

    def pll_fxcall(self, dae):
        pass

    def pll_gycall(self, dae):
        pass


class VSC1(VSC1_Common, VSC1_Outer1, Current1, PLL1):
    def __init__(self, system, name):
        VSC1_Common.__init__(self, system, name)
        VSC1_Outer1.__init__(self, system, name)
        Current1.__init__(self, system, name)
        PLL1.__init__(self, system, name)
        self._inst_meta()


class VSC2(DCBase):
    """The voltage-source type power-synchronization controlled VSC"""

    def __init__(self, system, name):
        super(VSC2, self).__init__(system, name)
        self._group = 'AC/DC'
        self._name = 'VSC2'
        self._ac = {'bus': ['a', 'v']}
        self._data.update({'vsc': None,
                           'Kp1': 0.1,
                           'Ki1': 1,
                           'Kp2': 0.1,
                           'Ki2': 1,
                           'Kp3': 1,
                           'Ki3': 1,
                           'Kp4': 1,
                           'Ki4': 1,
                           'KQ': 0.05,
                           'M': 6,
                           'D': 3,
                           'Tt': 0.01,
                           'wref0': 1,
                           })

        self._params.extend(['vsc', 'Kp1', 'Ki1', 'Kp2', 'Ki2', 'Kp3', 'Ki3', 'Kp4', 'Ki4',
                             'KQ', 'M', 'D', 'Tt', 'wref0'])
        self._descr.update({'Kp1': 'proportional gain of Id',
                            'Ki1': 'integral gain of Id',
                            'Kp2': 'proportional gain of Iq',
                            'Ki2': 'integral gain of Iq',
                            'Kp3': 'proportional gain of vd',
                            'Ki3': 'integral gain of vd',
                            'Kp4': 'proportional gain of vq',
                            'Ki4': 'integral gain of vq',
                            'KQ': 'reactive power droop',
                            'M': 'startup time constant of emulated mass',
                            'D': 'emulated damping',
                            'Tt': 'ac voltage measurement delay',
                            'wref0': 'frequency reference',
                            'vsc': 'static vsc idx',
                            })
        self._algebs.extend(['wref', 'vref', 'p', 'q', 'vd', 'vq',
                             'Idref', 'Iqref', 'udref', 'uqref',
                             ])
        self._states.extend(['Id', 'Iq', 'ud', 'uq',
                             'Md', 'Mq', 'Nd', 'Nq',
                             'adq', 'xw',
                             ])
        self._fnamey.extend(['\\omega^{ref}', 'V^{ref}', 'P', 'Q', 'U_d^s', 'U_q^s',
                             'I_d^{ref}', 'I_q^{ref}', 'U_d^{ref}', 'U_q^{ref}',
                             ])
        self._fnamex.extend(['I_d', 'I_q', 'u_c^d', 'u_^q', '\\theta_{dq}',
                             'M_d', 'M_q', 'Nd', 'Nq', 'x_\\omega',
                             ])
        self._mandatory.extend(['vsc'])
        self._zeros.extend(['Tt'])
        self._service.extend(['wn', 'iTt', 'iLsh', ])
        self.calls.update({'init1': True, 'gcall': True,
                           'fcall': True,
                           'gycall': True, 'fxcall': True,
                           'jac0': True,
                           })
        self._mandatory.remove('node1')
        self._mandatory.remove('node2')
        self._mandatory.remove('Vdcn')
        self._ac = {}
        self._dc = {}
        self._inst_meta()

    def setup(self):
        super(VSC2, self).setup()

    def init1(self, dae):
        self.copy_param('VSC', src='u', dest='u0', fkey=self.vsc)

        self.copy_param('VSC', src='Vn', fkey=self.vsc)
        self.copy_param('VSC', src='Vdcn', fkey=self.vsc)

        self.copy_param('VSC', src='PQ', fkey=self.vsc)
        self.copy_param('VSC', src='PV', fkey=self.vsc)
        self.copy_param('VSC', src='vV', fkey=self.vsc)
        self.copy_param('VSC', src='vQ', fkey=self.vsc)

        self.copy_param('VSC', src='rsh', fkey=self.vsc)
        self.copy_param('VSC', src='xsh', fkey=self.vsc)

        self.copy_param('VSC', src='a', fkey=self.vsc)
        self.copy_param('VSC', src='v', fkey=self.vsc)
        self.copy_param('VSC', src='ash', fkey=self.vsc)
        self.copy_param('VSC', src='vsh', fkey=self.vsc)
        self.copy_param('VSC', src='v1', fkey=self.vsc)
        self.copy_param('VSC', src='v2', fkey=self.vsc)

        self.copy_param('VSC', src='psh', dest='pref0', fkey=self.vsc)
        self.copy_param('VSC', src='qsh', dest='qref0', fkey=self.vsc)
        self.copy_param('VSC', src='pdc', dest='pdc', fkey=self.vsc)
        self.copy_param('VSC', src='bus', fkey=self.vsc)

        self.u = aandb(self.u, self.u0)
        self.PQ = mul(self.u, self.PQ)
        self.PV = mul(self.u, self.PV)
        self.vV = mul(self.u, self.vV)
        self.vQ = mul(self.u, self.vQ)

        self.pref0 = dae.y[self.pref0]
        self.qref0 = dae.y[self.qref0]
        self.vref0 = dae.y[self.v]
        self.vdcref0 = mul(self.vV + self.vQ, dae.y[self.v1] - dae.y[self.v2])

        self.iLsh = div(1.0, self.xsh)
        self.iTt = div(1.0, self.Tt)
        self.iM = div(self.u, self.M)
        self.iD = div(self.u, self.D)

        # start initialization
        dae.x[self.adq] = dae.y[self.a]
        dae.y[self.vd] = dae.y[self.v]
        dae.y[self.vq] = zeros(self.n, 1)

        Id = div(self.pref0, dae.y[self.vd])
        Iq = div(self.qref0, dae.y[self.vd])
        dae.y[self.Idref] = Id
        dae.y[self.Iqref] = Iq
        dae.x[self.Md] = mul(self.rsh, Id)
        dae.x[self.Mq] = mul(self.rsh, Iq)
        dae.x[self.Id] = Id
        dae.x[self.Iq] = Iq

        ud = dae.y[self.vd] + mul(self.xsh, Iq) + mul(self.rsh, Id)
        uq = dae.y[self.vq] - mul(self.xsh, Id) + mul(self.rsh, Iq)

        dae.y[self.udref] = ud
        dae.y[self.uqref] = uq
        dae.x[self.ud] = ud
        dae.x[self.uq] = uq

        dae.x[self.Nd] = Id
        dae.x[self.Nq] = Iq

        dae.y[self.p] = self.pref0
        dae.y[self.q] = self.qref0
        dae.y[self.vref] = self.vref0 - mul(self.KQ, self.qref0 - dae.y[self.q])
        dae.y[self.wref] = self.wref0

        for idx in self.vsc:
            self.system.VSC.disable(idx)

    def gcall(self, dae):
        dae.g[self.wref] = dae.y[self.wref] - self.wref0 - dae.x[self.xw]
        dae.g[self.vref] = dae.y[self.vref] - self.vref0 + mul(self.KQ, dae.y[self.q] - self.qref0)
        dae.g[self.p] = -dae.y[self.p] + mul(dae.x[self.Id], dae.y[self.vd]) + mul(dae.x[self.Iq], dae.y[self.vq])
        dae.g[self.q] = -dae.y[self.q] + mul(dae.x[self.Iq], dae.y[self.vd]) - mul(dae.x[self.Id], dae.y[self.vq])
        dae.g[self.vd] = -dae.y[self.vd] + mul(dae.y[self.v], cos(dae.y[self.a] - dae.x[self.adq]))
        dae.g[self.vq] = -dae.y[self.vq] - mul(dae.y[self.v], sin(dae.y[self.a] - dae.x[self.adq]))
        dae.g[self.Idref] = dae.x[self.Nd] - dae.y[self.Idref] + mul(self.Kp3, dae.y[self.vref] - dae.y[self.vd])
        dae.g[self.Iqref] = dae.x[self.Nq] - dae.y[self.Iqref] - mul(self.Kp4, dae.y[self.vq])
        dae.g[self.udref] = dae.x[self.Md] + dae.y[self.vd] - dae.y[self.udref] + mul(dae.y[self.Iqref],
                                                                                      self.xsh) + mul(self.Kp1, dae.y[
            self.Idref] - dae.x[self.Id])
        dae.g[self.uqref] = dae.x[self.Mq] + dae.y[self.vq] - dae.y[self.uqref] + mul(self.Kp2,
                                                                                      dae.y[self.Iqref] - dae.x[
                                                                                          self.Iq]) - mul(
            dae.y[self.Idref], self.xsh)
        dae.g += spmatrix(- dae.y[self.p], self.a, [0] * self.n, (dae.m, 1), 'd')
        dae.g += spmatrix(- dae.y[self.q], self.v, [0] * self.n, (dae.m, 1), 'd')
        dae.g += spmatrix(mul((dae.y[self.v1] - dae.y[self.v2]) ** -1,
                              mul(dae.x[self.Id], dae.x[self.ud]) + mul(dae.x[self.Iq], dae.x[self.uq])), self.v1,
                          [0] * self.n, (dae.m, 1), 'd')
        dae.g += spmatrix(mul((dae.y[self.v1] - dae.y[self.v2]) ** -1,
                              -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.v2,
                          [0] * self.n, (dae.m, 1), 'd')

    def fcall(self, dae):
        dae.f[self.Id] = -dae.x[self.Iq] + mul(self.iLsh, dae.x[self.ud] - dae.y[self.vd]) - mul(dae.x[self.Id],
                                                                                                 self.iLsh, self.rsh)
        dae.f[self.Iq] = dae.x[self.Id] + mul(self.iLsh, dae.x[self.uq] - dae.y[self.vq]) - mul(dae.x[self.Iq],
                                                                                                self.iLsh, self.rsh)
        dae.f[self.ud] = mul(self.iTt, dae.y[self.udref] - dae.x[self.ud])
        dae.f[self.uq] = mul(self.iTt, dae.y[self.uqref] - dae.x[self.uq])
        dae.f[self.Md] = mul(self.Ki1, dae.y[self.Idref] - dae.x[self.Id])
        dae.f[self.Mq] = mul(self.Ki2, dae.y[self.Iqref] - dae.x[self.Iq])
        dae.f[self.Nd] = mul(self.Ki3, dae.y[self.vref] - dae.y[self.vd])
        dae.f[self.Nq] = - mul(self.Ki4, dae.y[self.vq])
        dae.f[self.adq] = dae.y[self.wref] - self.wref0
        dae.f[self.xw] = mul(self.iM, self.pref0 - dae.y[self.p] - mul(self.D, dae.x[self.xw]))

    def gycall(self, dae):
        dae.add_jac(Gy, dae.x[self.Id], self.p, self.vd)
        dae.add_jac(Gy, dae.x[self.Iq], self.p, self.vq)
        dae.add_jac(Gy, dae.x[self.Iq], self.q, self.vd)
        dae.add_jac(Gy, - dae.x[self.Id], self.q, self.vq)
        dae.add_jac(Gy, - mul(dae.y[self.v], sin(dae.y[self.a] - dae.x[self.adq])), self.vd, self.a)
        dae.add_jac(Gy, cos(dae.y[self.a] - dae.x[self.adq]), self.vd, self.v)
        dae.add_jac(Gy, - mul(dae.y[self.v], cos(dae.y[self.a] - dae.x[self.adq])), self.vq, self.a)
        dae.add_jac(Gy, - sin(dae.y[self.a] - dae.x[self.adq]), self.vq, self.v)
        dae.add_jac(Gy, - mul((dae.y[self.v1] - dae.y[self.v2]) ** -2,
                              mul(dae.x[self.Id], dae.x[self.ud]) + mul(dae.x[self.Iq], dae.x[self.uq])), self.v1,
                    self.v1)
        dae.add_jac(Gy, mul((dae.y[self.v1] - dae.y[self.v2]) ** -2,
                            mul(dae.x[self.Id], dae.x[self.ud]) + mul(dae.x[self.Iq], dae.x[self.uq])), self.v1,
                    self.v2)
        dae.add_jac(Gy, - mul((dae.y[self.v1] - dae.y[self.v2]) ** -2,
                              -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.v2,
                    self.v1)
        dae.add_jac(Gy, mul((dae.y[self.v1] - dae.y[self.v2]) ** -2,
                            -mul(dae.x[self.Id], dae.x[self.ud]) - mul(dae.x[self.Iq], dae.x[self.uq])), self.v2,
                    self.v2)

    def fxcall(self, dae):
        dae.add_jac(Gx, dae.y[self.vd], self.p, self.Id)
        dae.add_jac(Gx, dae.y[self.vq], self.p, self.Iq)
        dae.add_jac(Gx, dae.y[self.vd], self.q, self.Iq)
        dae.add_jac(Gx, - dae.y[self.vq], self.q, self.Id)
        dae.add_jac(Gx, mul(dae.y[self.v], sin(dae.y[self.a] - dae.x[self.adq])), self.vd, self.adq)
        dae.add_jac(Gx, mul(dae.y[self.v], cos(dae.y[self.a] - dae.x[self.adq])), self.vq, self.adq)
        dae.add_jac(Gx, mul(dae.x[self.uq], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v1, self.Iq)
        dae.add_jac(Gx, mul(dae.x[self.Iq], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v1, self.uq)
        dae.add_jac(Gx, mul(dae.x[self.ud], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v1, self.Id)
        dae.add_jac(Gx, mul(dae.x[self.Id], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v1, self.ud)
        dae.add_jac(Gx, - mul(dae.x[self.uq], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v2, self.Iq)
        dae.add_jac(Gx, - mul(dae.x[self.Iq], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v2, self.uq)
        dae.add_jac(Gx, - mul(dae.x[self.ud], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v2, self.Id)
        dae.add_jac(Gx, - mul(dae.x[self.Id], (dae.y[self.v1] - dae.y[self.v2]) ** -1), self.v2, self.ud)

    def jac0(self, dae):
        dae.add_jac(Gy0, 1, self.wref, self.wref)
        dae.add_jac(Gy0, self.KQ, self.vref, self.q)
        dae.add_jac(Gy0, 1, self.vref, self.vref)
        dae.add_jac(Gy0, -1, self.p, self.p)
        dae.add_jac(Gy0, -1, self.q, self.q)
        dae.add_jac(Gy0, -1, self.vd, self.vd)
        dae.add_jac(Gy0, -1, self.vq, self.vq)
        dae.add_jac(Gy0, - self.Kp3, self.Idref, self.vd)
        dae.add_jac(Gy0, self.Kp3, self.Idref, self.vref)
        dae.add_jac(Gy0, -1, self.Idref, self.Idref)
        dae.add_jac(Gy0, - self.Kp4, self.Iqref, self.vq)
        dae.add_jac(Gy0, -1, self.Iqref, self.Iqref)
        dae.add_jac(Gy0, self.xsh, self.udref, self.Iqref)
        dae.add_jac(Gy0, 1, self.udref, self.vd)
        dae.add_jac(Gy0, -1, self.udref, self.udref)
        dae.add_jac(Gy0, self.Kp1, self.udref, self.Idref)
        dae.add_jac(Gy0, -1, self.uqref, self.uqref)
        dae.add_jac(Gy0, self.Kp2, self.uqref, self.Iqref)
        dae.add_jac(Gy0, - self.xsh, self.uqref, self.Idref)
        dae.add_jac(Gy0, 1, self.uqref, self.vq)
        dae.add_jac(Gy0, -1, self.a, self.p)
        dae.add_jac(Gy0, -1, self.v, self.q)
        dae.add_jac(Gx0, -1, self.wref, self.xw)
        dae.add_jac(Gx0, 1, self.Idref, self.Nd)
        dae.add_jac(Gx0, 1, self.Iqref, self.Nq)
        dae.add_jac(Gx0, 1, self.udref, self.Md)
        dae.add_jac(Gx0, - self.Kp1, self.udref, self.Id)
        dae.add_jac(Gx0, - self.Kp2, self.uqref, self.Iq)
        dae.add_jac(Gx0, 1, self.uqref, self.Mq)
        dae.add_jac(Fx0, -1, self.Id, self.Iq)
        dae.add_jac(Fx0, - mul(self.iLsh, self.rsh), self.Id, self.Id)
        dae.add_jac(Fx0, self.iLsh, self.Id, self.ud)
        dae.add_jac(Fx0, - mul(self.iLsh, self.rsh), self.Iq, self.Iq)
        dae.add_jac(Fx0, self.iLsh, self.Iq, self.uq)
        dae.add_jac(Fx0, 1, self.Iq, self.Id)
        dae.add_jac(Fx0, - self.iTt, self.ud, self.ud)
        dae.add_jac(Fx0, - self.iTt, self.uq, self.uq)
        dae.add_jac(Fx0, - self.Ki1, self.Md, self.Id)
        dae.add_jac(Fx0, - self.Ki2, self.Mq, self.Iq)
        dae.add_jac(Fx0, - mul(self.D, self.iM), self.xw, self.xw)
        dae.add_jac(Fy0, - self.iLsh, self.Id, self.vd)
        dae.add_jac(Fy0, - self.iLsh, self.Iq, self.vq)
        dae.add_jac(Fy0, self.iTt, self.ud, self.udref)
        dae.add_jac(Fy0, self.iTt, self.uq, self.uqref)
        dae.add_jac(Fy0, self.Ki1, self.Md, self.Idref)
        dae.add_jac(Fy0, self.Ki2, self.Mq, self.Iqref)
        dae.add_jac(Fy0, - self.Ki3, self.Nd, self.vd)
        dae.add_jac(Fy0, self.Ki3, self.Nd, self.vref)
        dae.add_jac(Fy0, - self.Ki4, self.Nq, self.vq)
        dae.add_jac(Fy0, 1, self.adq, self.wref)
        dae.add_jac(Fy0, - self.iM, self.xw, self.p)


class VSC3(DCBase):
    """Simplified voltage-source type power synchronizing control VSC """

    def __init__(self, system, name):
        super(VSC3, self).__init__(system, name)
        self._group = 'AC/DC'
        self._name = 'VSC3'
        self._data.update({'vsc': None,
                           'Kp1': 0.2,
                           'Ki1': 1,
                           'Kp2': 0.2,
                           'Ki2': 1,
                           'KQ': 0.05,
                           'M': 6,
                           'D': 3,
                           'Tt': 0.01,
                           'wref0': 1,
                           })

        self._params.extend(['vsc', 'Kp1', 'Ki1', 'Kp2', 'Ki2', 'KQ', 'M', 'D', 'Tt', 'wref0'])
        self._descr.update({'Kp1': 'proportional gain of Id',
                            'Ki1': 'integral gain of Id',
                            'Kp2': 'proportional gain of Iq',
                            'Ki2': 'integral gain of Iq',
                            'KQ': 'reactive power droop',
                            'M': 'startup time constant of emulated mass',
                            'D': 'emulated damping',
                            'Tt': 'ac voltage measurement delay',
                            'wref0': 'frequency reference',
                            'vsc': 'static vsc idx',
                            })
        self._algebs.extend(['wref', 'vref', 'p', 'q', 'vd', 'vq',
                             'Idref', 'Iqref', 'udref', 'uqref',
                             ])
        self._states.extend(['Id', 'Iq', 'ud', 'uq',
                             'Md', 'Mq', 'Nd', 'Nq',
                             'adq', 'xw',
                             ])
        self._fnamey.extend(['\\omega^{ref}', 'V^{ref}', 'P', 'Q', 'U_d^s', 'U_q^s',
                             'I_d^{ref}', 'I_q^{ref}', 'U_d^{ref}', 'U_q^{ref}',
                             ])
        self._fnamex.extend(['I_d', 'I_q', 'u_c^d', 'u_^q', '\\theta_{dq}',
                             'M_d', 'M_q', 'Nd', 'Nq', 'x_\\omega',
                             ])
        self._zeros.extend(['Tt'])
        self._service.extend(['wn', 'iTt', 'iLsh', ])
        self.calls.update({'init1': True, 'gcall': True,
                           'fcall': True, 'jac0': True,
                           'gycall': True, 'fxcall': True,
                           })
        self._mandatory.extend(['vsc'])
        self._mandatory.remove('node1')
        self._mandatory.remove('node2')
        self._mandatory.remove('Vdcn')
        self._ac = {}
        self._dc = {}
        self._inst_meta()

    def setup(self):
        super(VSC3, self).setup()

    def init1(self, dae):
        self.copy_param('VSC', src='u', dest='u0', fkey=self.vsc)
        self.copy_param('VSC', src='rsh', fkey=self.vsc)
        self.copy_param('VSC', src='xsh', fkey=self.vsc)
        self.copy_param('VSC', src='ash', fkey=self.vsc)
        self.copy_param('VSC', src='vsh', fkey=self.vsc)
        self.copy_param('VSC', src='psh', dest='pref0', fkey=self.vsc)
        self.copy_param('VSC', src='qsh', dest='qref0', fkey=self.vsc)

        self.copy_param('VSC', src='pdc', fkey=self.vsc)
        self.copy_param('VSC', src='bus', fkey=self.vsc)
        self.copy_param('VSC', src='a', fkey=self.vsc)
        self.copy_param('VSC', src='v', fkey=self.vsc)
        self.copy_param('VSC', src='v1', fkey=self.vsc)
        self.copy_param('VSC', src='v2', fkey=self.vsc)

        self.u = aandb(self.u, self.u0)

        self.pref0 = dae.y[self.pref0]
        self.qref0 = dae.y[self.qref0]
        self.vref0 = dae.y[self.v]
        self.vdcref0 = dae.y[self.v1] - dae.y[self.v2]

        self.iLsh = div(1.0, self.xsh)
        self.iTt = div(1.0, self.Tt)
        self.iM = div(self.u, self.M)
        self.iD = div(self.u, self.D)

        # start initialization
        dae.x[self.adq] = dae.y[self.a]
        dae.y[self.vd] = dae.y[self.v]
        dae.y[self.vq] = zeros(self.n, 1)

        Id = div(self.pref0, dae.y[self.vd])
        Iq = div(self.qref0, dae.y[self.vd])
        dae.y[self.Idref] = Id
        dae.y[self.Iqref] = Iq
        dae.x[self.Md] = mul(self.rsh, Id)
        dae.x[self.Mq] = mul(self.rsh, Iq)
        dae.x[self.Id] = Id
        dae.x[self.Iq] = Iq

        ud = dae.y[self.vd] + mul(self.xsh, Iq) + mul(self.rsh, Id)
        uq = dae.y[self.vq] - mul(self.xsh, Id) + mul(self.rsh, Iq)

        dae.y[self.udref] = ud
        dae.y[self.uqref] = uq
        dae.x[self.ud] = ud
        dae.x[self.uq] = uq

        dae.x[self.Nd] = Id
        dae.x[self.Nq] = Iq

        dae.y[self.p] = self.pref0
        dae.y[self.q] = self.qref0
        dae.y[self.vref] = self.vref0 - mul(self.KQ, self.qref0 - dae.y[self.q])
        dae.y[self.wref] = self.wref0

        for idx in self.vsc:
            self.system.VSC.disable(idx)


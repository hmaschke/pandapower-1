# -*- coding: utf-8 -*-

# Copyright 1996-2015 PSERC. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

# Copyright (c) 2016-2021 by University of Kassel and Fraunhofer Institute for Energy Economics
# and Energy System Technology (IEE), Kassel. All rights reserved.


"""Solves the power flow using a full Newton's method.
"""

from numpy import angle, sqrt, exp, linalg, conj, real, r_, Inf, arange, zeros, ones, max, zeros_like, column_stack, float64, array, square
from scipy.sparse.linalg import spsolve

from pandapower.pf.iwamoto_multiplier import _iwamoto_step
from pandapower.pypower.makeSbus import makeSbus
from pandapower.pf.create_jacobian import create_jacobian_matrix, get_fastest_jacobian_function
from pandapower.pypower.idx_gen import PG
from pandapower.pypower.idx_bus import PD, SL_FAC, BASE_KV
from pandapower.pypower.idx_brch import BR_R, F_BUS, BR_R_OHM_PER_KM

from pandapower.tdpf.create_jacobian_tdpf import calc_a0_a1_a2_tau, create_J_tdpf, get_S_flows, calc_I


def newtonpf(Ybus, Sbus, V0, ref, pv, pq, ppci, options, makeYbus=None):
    """Solves the power flow using a full Newton's method.
    Solves for bus voltages given the full system admittance matrix (for
    all buses), the complex bus power injection vector (for all buses),
    the initial vector of complex bus voltages, and column vectors with
    the lists of bus indices for the swing bus, PV buses, and PQ buses,
    respectively. The bus voltage vector contains the set point for
    generator (including ref bus) buses, and the reference angle of the
    swing bus, as well as an initial guess for remaining magnitudes and
    angles.
    @see: L{runpf}
    @author: Ray Zimmerman (PSERC Cornell)
    @author: Richard Lincoln
    Modified by University of Kassel (Florian Schaefer) to use numba
    """

    # options
    tol = options['tolerance_mva']
    max_it = options["max_iteration"]
    numba = options["numba"]
    iwamoto = options["algorithm"] == "iwamoto_nr"
    voltage_depend_loads = options["voltage_depend_loads"]
    dist_slack = options["distributed_slack"]
    v_debug = options["v_debug"]
    use_umfpack = options["use_umfpack"]
    permc_spec = options["permc_spec"]

    baseMVA = ppci['baseMVA']
    bus = ppci['bus']
    gen = ppci['gen']
    branch = ppci['branch']
    slack_weights = bus[:, SL_FAC].astype(float64)  ## contribution factors for distributed slack
    tdpf = options.get('tdpf', False)
    tdpf_delay_s = options.get('tdpf_delay_s')

    # initialize
    i = 0
    V = V0
    Va = angle(V)
    Vm = abs(V)
    dVa, dVm = None, None
    if iwamoto:
        dVm, dVa = zeros_like(Vm), zeros_like(Va)

    if v_debug:
        Vm_it = Vm.copy()
        Va_it = Va.copy()
    else:
        Vm_it = None
        Va_it = None

    # set up indexing for updating V
    if dist_slack and len(ref) > 1:
        pv = r_[ref[1:], pv]
        ref = ref[[0]]

    pvpq = r_[pv, pq]
    # reference buses are always at the top, no matter where they are in the grid (very confusing...)
    # so in the refpvpq, the indices must be adjusted so that ref bus(es) starts with 0
    # todo: is it possible to simplify the indices/lookups and make the code clearer?
    # for columns: columns are in the normal order in Ybus; column numbers for J are reduced by 1 internally
    refpvpq = r_[ref, pvpq]
    # generate lookup pvpq -> index pvpq (used in createJ):
    #   shows for a given row from Ybus, which row in J it becomes
    #   e.g. the first row in J is a PV bus. If the first PV bus in Ybus is in the row 2, the index of the row in Jbus must be 0.
    #   pvpq_lookup will then have a 0 at the index 2
    pvpq_lookup = zeros(max(Ybus.indices) + 1, dtype=int)
    if dist_slack:
        # slack bus is relevant for the function createJ_ds
        pvpq_lookup[refpvpq] = arange(len(refpvpq))
    else:
        pvpq_lookup[pvpq] = arange(len(pvpq))

    pq_lookup = zeros(len(pvpq) + 1, dtype=int)
    pq_lookup[pq] = arange(len(pq))

    # get jacobian function
    createJ = get_fastest_jacobian_function(pvpq, pq, numba, dist_slack)

    nref = len(ref)
    npv = len(pv)
    npq = len(pq)
    j1 = 0
    j2 = npv  # j1:j2 - V angle of pv buses
    j3 = j2
    j4 = j2 + npq  # j3:j4 - V angle of pq buses
    j5 = j4
    j6 = j4 + npq  # j5:j6 - V mag of pq buses
    j7 = j6
    j8 = j6 + nref # j7:j8 - slacks

    T_base = 100  # T in p.u. for better convergence
    T_ref = 20
    # todo: enable using T0 as a start of previous time step?
    T0 = ones(shape=len(branch)) * T_ref  # todo: consider lookups line/trafo, in_service etc.
    T = T0 / T_base
    r_ref_pu = branch[:, BR_R].real.copy()
    v_base = bus[real(branch[:, F_BUS]).astype(int), BASE_KV]
    z_base_ohm = square(v_base) / baseMVA
    i_base_a = baseMVA / (v_base * sqrt(3))*1e3
    r_ref_ohm_per_m = 1e-3 * branch[:, BR_R_OHM_PER_KM].real.copy()
    alpha = ones(shape=len(branch)) * 0.004 # todo parameter ppc
    t_amb = 40  # todo parameter ppc

    # make initial guess for the slack
    slack = gen[:, PG].sum() - bus[:, PD].sum()
    # evaluate F(x0)
    F = _evaluate_Fx(Ybus, V, Sbus, ref, pv, pq, slack_weights, dist_slack, slack)
    if tdpf:
        Ybus, Yf, Yt = makeYbus(baseMVA, bus, branch)
        # todo: use parameters in ppc
        a0, a1, a2, tau = calc_a0_a1_a2_tau(t_amb=t_amb, t_max=90, r_ref_ohm_per_m=r_ref_ohm_per_m, conductor_outer_diameter_m=18.2e-3,
                                            mc_joule_per_m_k=525, v_m_per_s=0.5, wind_angle_degree=45, s_w_per_square_meter=1000)
        Sf, St, f_bus, _ = get_S_flows(branch, Yf, Yt, baseMVA, V)
        I = calc_I(Sf, bus, f_bus, V)
        i_pu = I / i_base_a
        # initial guess for T:
        T = _calc_T(V, I, a0, a1, a2, tau, tdpf_delay_s, T0) / T_base
        F_t = _evaluate_dT(V, T * T_base, I, a0, a1, a2, tau, tdpf_delay_s, T0)
        F = r_[F, F_t / T_base]
    converged = _check_for_convergence(F, tol)

    Ybus = Ybus.tocsr()
    J = None


    # do Newton iterations
    while (not converged and i < max_it):
        # update iteration counter
        i = i + 1

        if tdpf:
            # update the R and the Y-matrices
            # todo: f and t for lines only
            branch[:, BR_R] = r_ref_pu * (1 + alpha * (T * T_base - T_ref))
            Ybus, Yf, Yt = makeYbus(baseMVA, bus, branch)

        J = create_jacobian_matrix(Ybus, V, ref, refpvpq, pvpq, pq, createJ, pvpq_lookup, nref, npv, npq, numba, slack_weights, dist_slack)

        if tdpf:
            # p.u. values for T, a1, a2, I, S
            J = create_J_tdpf(branch, alpha, r_ref_pu, pvpq, pq, pvpq_lookup, pq_lookup, tau, tdpf_delay_s,
                              a1 * i_base_a ** 2 / T_base, a2 * i_base_a ** 4 / T_base, V, Sf / baseMVA, St / baseMVA, i_pu, J)

        dx = -1 * spsolve(J, F, permc_spec=permc_spec, use_umfpack=use_umfpack)
        # update voltage
        if npv and not iwamoto:
            Va[pv] = Va[pv] + dx[j1:j2]
        if npq and not iwamoto:
            Va[pq] = Va[pq] + dx[j3:j4]
            Vm[pq] = Vm[pq] + dx[j5:j6]
        if dist_slack:
            slack = slack + dx[j7:j8]
        if tdpf:
            T = T + dx[j7:]

        # iwamoto multiplier to increase convergence
        if iwamoto and not tdpf:
            Vm, Va = _iwamoto_step(Ybus, J, F, dx, pq, npv, npq, dVa, dVm, Vm, Va, pv, j1, j2, j3, j4, j5, j6)

        V = Vm * exp(1j * Va)
        Vm = abs(V)  # update Vm and Va again in case
        Va = angle(V)  # we wrapped around with a negative Vm

        if v_debug:
            Vm_it = column_stack((Vm_it, Vm))
            Va_it = column_stack((Va_it, Va))

        if voltage_depend_loads:
            Sbus = makeSbus(baseMVA, bus, gen, vm=Vm)

        F = _evaluate_Fx(Ybus, V, Sbus, ref, pv, pq, slack_weights, dist_slack, slack)

        if tdpf:
            Sf, St, f_bus, _ = get_S_flows(branch, Yf, Yt, baseMVA, V)
            I = calc_I(Sf, bus, f_bus, V)
            i_pu = I / i_base_a
            # T = _calc_T(V, T, I, a0, a1, a2, tau, tdpf_delay_s, T0)
            F_t = _evaluate_dT(V, T * T_base, I, a0, a1, a2, tau, tdpf_delay_s, T0)
            F = r_[F, F_t / T_base]

        converged = _check_for_convergence(F, tol)

    return V, converged, i, J, Vm_it, Va_it, T * T_base


def _calc_T(V, I, a0, a1, a2, tau, tdpf_delay_s, t_0_degree):
    t_ss = a0 + a1 * I ** 2 + a2 * I ** 4
    if tdpf_delay_s is not None:
        t_transient = t_ss - (t_ss - t_0_degree) * exp(-tdpf_delay_s / tau)
        return t_transient
    return t_ss


def _evaluate_dT(V, T, I, a0, a1, a2, tau, tdpf_delay_s, t_0_degree):
    t_calc = _calc_T(V, I, a0, a1, a2, tau, tdpf_delay_s, t_0_degree)
    return t_calc - T


def _evaluate_Fx(Ybus, V, Sbus, ref, pv, pq, slack_weights=None, dist_slack=False, slack=None):
    # evalute F(x)
    if dist_slack:
        # we include the slack power (slack * contribution factors) in the mismatch calculation
        mis = V * conj(Ybus * V) - Sbus + slack_weights * slack
        F = r_[mis[ref].real, mis[pv].real, mis[pq].real, mis[pq].imag]
    else:
        mis = V * conj(Ybus * V) - Sbus
        F = r_[mis[pv].real, mis[pq].real, mis[pq].imag]
    return F


def _check_for_convergence(F, tol):
    # calc infinity norm
    return linalg.norm(F, Inf) < tol

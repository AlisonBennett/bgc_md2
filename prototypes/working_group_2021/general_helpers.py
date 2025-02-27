import numpy as np
from tqdm import tqdm
from typing import Callable, Tuple, Iterable
from functools import reduce, lru_cache
from copy import copy
from time import time
from sympy import var, Symbol, sin, Min, Max, pi, integrate, lambdify
import CompartmentalSystems.helpers_reservoir as hr
from pathlib import Path
import json 
import netCDF4 as nc

days_per_year = 365 

# should be part  of CompartmentalSystems
def make_B_u_funcs(
        mvs,
        mpa,
        func_dict
    ):
        symbol_names = mvs.get_BibInfo().sym_dict.keys()   
        for name in symbol_names:
            var(name)
        t = mvs.get_TimeSymbol()
        it = Symbol('it')
        delta_t=Symbol('delta_t')
        model_params = {Symbol(k): v for k,v in mpa._asdict().items()}
        parameter_dict = {**model_params,delta_t: 1}
        state_vector = mvs.get_StateVariableTuple()

        sym_B =hr.euler_forward_B_sym(
                mvs.get_CompartmentalMatrix(),
                cont_time=t,
                delta_t=delta_t,
                iteration=it
        )
        sym_u = hr.euler_forward_net_u_sym(
                mvs.get_InputTuple(),
                t,
                delta_t,
                it
        )
        
        B_func = hr.numerical_array_func(
                state_vector = state_vector, 
                time_symbol=it,
                expr=sym_B,
                parameter_dict=parameter_dict,
                func_dict=func_dict
        )
        u_func = hr.numerical_array_func(
                state_vector = state_vector,
                time_symbol=it,
                expr=sym_u,
                parameter_dict=parameter_dict,
                func_dict=func_dict
        )
        return (B_func,u_func)


def make_uniform_proposer(
        c_max: Iterable,
        c_min: Iterable,
        D: float,
        filter_func: Callable[[np.ndarray], bool],
) -> Callable[[Iterable], Iterable]:
    """Returns a function that will be used by the mcmc algorithm to propose
    a new parameter value tuple based on a given one. 
    The two arrays c_max and c_min define the boundaries
    of the n-dimensional rectangular domain for the parameters and must be of
    the same shape.  After a possible parameter value has been sampled the
    filter_func will be applied to it to either accept or discard it.  So
    filter func must accept parameter array and return either True or False
    :param c_max: array of maximum parameter values
    :param c_min: array of minimum parameter values
    :param D: a parameter to regulate the proposer step. Higher D means smaller step size
    :param filter_func: model-specific function to filter out impossible parameter combinations
    """

    g = np.random.default_rng()

    def GenerateParamValues(c_op):
        paramNum = len(c_op)
        keep_searching = True
        while keep_searching:
            c_new = c_op + (g.random(paramNum) - 0.5) * (c_max - c_min) / D
            if filter_func(c_new):
                keep_searching = False
        return c_new

    return GenerateParamValues


def make_multivariate_normal_proposer(
        covv: np.ndarray,
        filter_func: Callable[[Iterable], bool],
) -> Callable[[Iterable], Iterable]:
    """Returns a function that will be used by mcmc algorithm to propose
    a new parameter(tuple) based on a given one.
    :param covv: The covariance matrix (usually estimated from a previously run chain)
    :param filter_func: model-specific function to filter out impossible parameter combinations
    """

    def GenerateParamValues(c_op):
        flag = True
        while flag:
            c_new = c_op + np.random.multivariate_normal(np.zeros(len(c_op)), covv)
            if filter_func(c_new):
                flag = False
        return c_new

    return GenerateParamValues


def accept_costfunction(J_last: float, J_new: float, K=1):
    """Regulates how new cost functions are accepted or rejected. If the new cost function is lower than the old one,
    it is always accepted. If the the new cost function is higher than the old one, it has a random
    chance to be accepted based on percentage difference between the old and the new. The chance is defined
    by an exponential function and regulated by the K coefficient.
    :param J_last: old (last accepted) cost function
    :param J_new: new cost function
    :param K: regulates acceptance chance. Default 1 means that a 1% higher cost function has 37% chance to be accepted.
    Increase K to reduce the chance to accept higher cost functions
    """
    accept = False
    delta_J_percent = (J_last - J_new) / J_last * 100  # normalize delta_J as a percentage of current J
    randNum = np.random.uniform(0, 1)
    if min(1.0, np.exp(delta_J_percent*K)) > randNum:  # 1% higher cost function has 37% chance to be accepted
        accept = True
    return accept


# Autostep MCMC: with uniform proposer modifying its step every 100 iterations depending on acceptance rate
def autostep_mcmc(
        initial_parameters: Iterable,
        filter_func: Callable,
        param2res: Callable[[np.ndarray], np.ndarray],
        costfunction: Callable[[np.ndarray], np.float64],
        nsimu: int,
        c_max: np.ndarray,
        c_min: np.ndarray,
        acceptance_rate=10,
        chunk_size=100,
        D_init=1
) -> Tuple[np.ndarray, np.ndarray]:
    """
    performs the Markov chain Monte Carlo simulation an returns a tuple of the array of sampled parameter(tuples)
    with shape (len(initial_parameters),nsimu) and the array of costfunction values with shape (q,nsimu)

    :param initial_parameters: The initial guess for the parameter (tuple) to be estimated
    :param filter_func: model-specific function to filter out impossible parameter combinations
    :param param2res: A function that given a parameter(tuple) returns
    the model output, which has to be an array of the same shape as the observations used to
    build the cost function.
    :param costfunction: A function that given a model output returns a real number. It is assumed to be created for a
    specific set of observations, which is why they do not appear as an argument.
    :param nsimu: The length of the chain
    :param c_max: Array of maximum values for each parameter
    :param c_min: Array of minimum values for each parameter
    :param acceptance_rate: Target acceptance rate in %, default is 10%
    :param chunk_size: number of iterations for which current acceptance ratio is assessed to modify the proposer step
    Set to 0 for constant step size. Default is 100.
    :param D_init: initial D value (Increase to get a smaller step size), default = 1
    """
    np.random.seed(seed=10)

    paramNum = len(initial_parameters)

    upgraded = 0
    C_op = initial_parameters
    tb = time()
    first_out = param2res(C_op)
    J_last = costfunction(first_out)
    J_min = J_last
    J_min_simu = 0
    print('first_iteration done after ' + str(time() - tb))
    # J_last = 400 # original code

    # initialize the result arrays to the maximum length
    # Depending on many of the parameters will be accepted only
    # a part of them will be filled with real values
    C_upgraded = np.zeros((paramNum, nsimu))
    J_upgraded = np.zeros((2, nsimu))
    D = D_init
    proposer = make_uniform_proposer(c_max=c_max, c_min=c_min, D=D * paramNum, filter_func=filter_func)
    # for simu in tqdm(range(nsimu)):
    st = time()
    accepted_current = 0
    if chunk_size == 0:
        chunk_size = nsimu  # if chunk_size is set to 0 - proceed without updating step size.
    for simu in range(nsimu):
        if (simu > 0) and (simu % chunk_size == 0):  # every chunk size (e.g. 100 iterations) update the proposer step
            if accepted_current == 0:
                accepted_current = 1  # to avoid division by 0
            D = D * np.sqrt(
                acceptance_rate / (accepted_current / chunk_size * 100))  # compare acceptance and update step
            accepted_current = 0
            proposer = make_uniform_proposer(c_max=c_max, c_min=c_min, D=D * paramNum, filter_func=filter_func)
        if simu % (chunk_size * 20) == 0:  # every 20 chunks - return to the initial step size (to avoid local minimum)
            D = D_init
        c_new = proposer(C_op)
        out_simu = param2res(c_new)
        J_new = costfunction(out_simu)

        if accept_costfunction (J_last=J_last, J_new=J_new):
            C_op = c_new
            J_last = J_new
            if J_last < J_min:
                J_min = J_last
                J_min_simu = simu
            C_upgraded[:, upgraded] = C_op
            J_upgraded[1, upgraded] = J_last
            J_upgraded[0, upgraded] = simu
            upgraded = upgraded + 1
            accepted_current = accepted_current + 1

        # print some metadata
        # (This could be added to the output file later)
        if simu % 10 == 0 or simu == (nsimu - 1):
            print(
                """ 
               #(upgraded): {n}  | D value: {d} 
               overall acceptance ratio: {r}% | currently {ac} accepted out of {ch}
               progress: {simu:05d}/{nsimu:05d} {pbs} {p:02d}%
               time elapsed: {minutes:02d}:{sec:02d}
               overall minimum cost: {cost} achieved at {s} iteration | last accepted cost: {cost2} 
               """.format(
                    n=upgraded,
                    r=int(upgraded / (simu + 1) * 100),
                    simu=simu,
                    nsimu=nsimu,
                    pbs='|' + int(50 * simu / (nsimu - 1)) * '#' + int((1 - simu / (nsimu - 1)) * 50) * ' ' + '|',
                    p=int(simu / (nsimu - 1) * 100),
                    minutes=int((time() - st) / 60),
                    sec=int((time() - st) % 60),
                    cost=round(J_min, 2),
                    cost2=round(J_last, 2),
                    ac=accepted_current,
                    # rr=int(accepted_current / chunk_size * 100),
                    ch=chunk_size,
                    d=round(D, 3),
                    s=J_min_simu
                ),
                end='\033[5A'  # print always on the same spot of the screen...
            )

    # remove the part of the arrays that is still filled with zeros
    useful_slice = slice(0, upgraded)
    return C_upgraded[:, useful_slice], J_upgraded[:, useful_slice]

# Adaptive MCMC: with multivariate normal proposer based on adaptive covariance matrix
def adaptive_mcmc(
        initial_parameters: Iterable,
        covv: np.ndarray,
        filter_func: Callable,
        param2res: Callable[[np.ndarray], np.ndarray],
        costfunction: Callable[[np.ndarray], np.float64],
        nsimu: int,
        sd_controlling_factor=10
) -> Tuple[np.ndarray, np.ndarray]:
    """
    performs the Markov chain Monte Carlo simulation an returns a tuple of the array of sampled parameter(tuples) with
    shape (len(initial_parameters),nsimu) and the array of cost function values with shape (q,nsimu)
    :param initial_parameters: The initial guess for the parameter (tuple) to be estimated
    :param covv: The covariance matrix (usually estimated from a previously run chain)
    :param filter_func: function to remove impossible parameter combinations
    :param param2res: A function that given a parameter(tuple) returns
    the model output, which has to be an array of the same shape as the observations used to
    build the cost function.
    :param costfunction: A function that given a model output returns a real number. It is assumed to be created for
    a specific set of observations, which is why they do not appear as an argument.
    :param nsimu: The length of the chain
    :param sd_controlling_factor: optional parameter to scale the covariance matrix. Increase to get a smaller step size
    """

    np.random.seed(seed=10)

    paramNum = len(initial_parameters)

    sd = 1 / sd_controlling_factor / paramNum
    covv = covv * sd

    proposer = make_multivariate_normal_proposer(covv, filter_func)

    upgraded = 0
    C_op = initial_parameters
    tb = time()
    first_out = param2res(C_op)

    J_last = costfunction(first_out)
    J_min = J_last
    J_min_simu = 0
    print('first_iteration done after ' + str(time() - tb))
    # J_last = 400 # original code

    # initialize the result arrays to the maximum length
    # Depending on many of the parameters will be accepted only
    # a part of them will be filled with real values
    C_upgraded = np.zeros((paramNum, nsimu))
    J_upgraded = np.zeros((2, nsimu))

    # for simu in tqdm(range(nsimu)):
    st = time()
    #  from IPython import embed;embed()
    for simu in range(nsimu):
        # if (upgraded%10 == 0) & (upgraded > nsimu/20):
        if simu > nsimu / 10:
            covv = sd * np.cov(C_accepted)
            proposer = make_multivariate_normal_proposer(covv, filter_func)
        c_new = proposer(C_op)
        out_simu = param2res(c_new)
        J_new = costfunction(out_simu)

        if accept_costfunction(J_last=J_last, J_new=J_new):
            C_op = c_new
            J_last = J_new
            if J_last < J_min:
                J_min = J_last
                J_min_simu = simu
            C_upgraded[:, upgraded] = C_op
            C_accepted = C_upgraded[:, 0:upgraded]
            J_upgraded[1, upgraded] = J_last
            J_upgraded[0, upgraded] = simu
            upgraded = upgraded + 1
        # print some metadata
        # (This could be added to the output file later)

        if simu % 10 == 0 or simu == (nsimu - 1):
            print(
                """ 
#(upgraded): {n}
overall acceptance ratio till now: {r}% 
progress: {simu:05d}/{nsimu:05d} {pbs} {p:02d}%
time elapsed: {minutes:02d}:{sec:02d}
overall minimum cost: {cost} achieved at {s} iteration | last accepted cost: {cost2} 
""".format(
                    n=upgraded,
                    r=int(upgraded / (simu + 1) * 100),
                    simu=simu,
                    nsimu=nsimu,
                    pbs='|' + int(50 * simu / (nsimu - 1)) * '#' + int((1 - simu / (nsimu - 1)) * 50) * ' ' + '|',
                    p=int(simu / (nsimu - 1) * 100),
                    minutes=int((time() - st) / 60),
                    sec=int((time() - st) % 60),
                    cost=round(J_min, 2),
                    cost2=round(J_last, 2),
                    s=J_min_simu
                ),
                end='\033[5A'  # print always on the same spot of the screen...
            )

    # remove the part of the arrays that is still filled with zeros
    useful_slice = slice(0, upgraded)
    return C_upgraded[:, useful_slice], J_upgraded[:, useful_slice]


def mcmc(
        initial_parameters: Iterable,
        proposer: Callable[[Iterable], Iterable],
        param2res: Callable[[np.ndarray], np.ndarray],
        costfunction: Callable[[np.ndarray], np.float64],
        nsimu: int
) -> Tuple[np.ndarray, np.ndarray]:
    """
    performs the Markov chain Monte Carlo simulation an returns a tuple of the array of sampled parameter(tuples) with
    shape (len(initial_parameters),nsimu) and the array of costfunction values with shape (q,nsimu)

    :param initial_parameters: The initial guess for the parameter (tuple) to be estimated
    :param proposer: A function that proposes a new parameter(tuple) from a given parameter (tuple).
    :param param2res: A function that given a parameter(tuple) returns
    the model output, which has to be an array of the same shape as the observations used to
    build the costfunction.
    :param costfunction: A function that given a model output returns a real number. It is assumed to be created for
    a specific set of observations, which is why they do not appear as an argument.
    :param nsimu: The length of the chain
    """
    np.random.seed(seed=10)

    paramNum = len(initial_parameters)

    upgraded = 0
    C_op = initial_parameters
    tb = time()
    first_out = param2res(C_op)
    J_last = costfunction(first_out)
    J_min = J_last
    J_min_simu = 0
    print('first_iteration done after ' + str(time() - tb))
    # J_last = 400 # original code

    # initialize the result arrays to the maximum length
    # Depending on many of the parameters will be accepted only 
    # a part of them will be filled with real values
    C_upgraded = np.zeros((paramNum, nsimu))
    J_upgraded = np.zeros((2, nsimu))

    # for simu in tqdm(range(nsimu)):
    st = time()
    for simu in range(nsimu):
        c_new = proposer(C_op)
        out_simu = param2res(c_new)
        J_new = costfunction(out_simu)

        if accept_costfunction(J_last=J_last, J_new=J_new):
            C_op = c_new
            J_last = J_new
            if J_last < J_min:
                J_min = J_last
                J_min_simu = simu
            C_upgraded[:, upgraded] = C_op
            J_upgraded[1, upgraded] = J_last
            J_upgraded[0, upgraded] = simu
            upgraded = upgraded + 1
        # print some metadata 
        # (This could be added to the output file later)

        if simu % 10 == 0 or simu == (nsimu - 1):
            print(
                """ 
#(upgraded): {n}
overall acceptance ratio till now: {r}% 
progress: {simu:05d}/{nsimu:05d} {pbs} {p:02d}%
time elapsed: {minutes:02d}:{sec:02d}
overall minimum cost: {cost} achieved at {s} iteration | last accepted cost: {cost2} 
""".format(
                    n=upgraded,
                    r=int(upgraded / (simu + 1) * 100),
                    simu=simu,
                    nsimu=nsimu,
                    pbs='|' + int(50 * simu / (nsimu - 1)) * '#' + int((1 - simu / (nsimu - 1)) * 50) * ' ' + '|',
                    p=int(simu / (nsimu - 1) * 100),
                    minutes=int((time() - st) / 60),
                    sec=int((time() - st) % 60),
                    cost=round(J_min, 2),
                    cost2=round(J_last, 2),
                    s=J_min_simu
                ),
                end='\033[5A'  # print always on the same spot of the screen...
            )

    # remove the part of the arryas that is still filled with zeros
    useful_slice = slice(0, upgraded)
    return C_upgraded[:, useful_slice], J_upgraded[:, useful_slice]


def make_feng_cost_func(
        obs: np.ndarray,
) -> Callable[[np.ndarray], np.float64]:
    # Note:
    # in our code the dimension 0 is the time
    # and dimension 1 the pool index
    means = obs.mean(axis=0)
    mean_centered_obs = obs - means
    # now we compute a scaling factor per observable stream
    # fixme mm 10-28-2021
    #   The denominators in this case are actually the TEMPORAL variances of the data streams
    denominators = np.sum(mean_centered_obs ** 2, axis=0)

    #   The desired effect of automatically adjusting weight could be achieved
    #   by the mean itself.
    # dominators = means
    def costfunction(mod: np.ndarray) -> np.float64:
        cost = np.mean(
            np.sum((obs - mod) ** 2, axis=0) / denominators * 100
        )
        return cost

    return costfunction


def make_jon_cost_func(
        obs: np.ndarray,
) -> Callable[[np.ndarray], np.float64]:
    # Note:
    # in our code the dimension 0 is the time
    # and dimension 1 the pool index
    n = obs.shape[0]
    means = obs.mean(axis=0)
    denominators = means ** 2

    def costfunction(mod: np.ndarray) -> np.float64:
        cost = np.mean(
            (100/n) * np.sum((obs - mod) ** 2, axis=0) / denominators)
        return cost

    return costfunction


def day_2_month_index(d):
    return months_by_day_arr()[(d % days_per_year)]


@lru_cache
def months_by_day_arr():
    days_per_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return np.concatenate(
        tuple(
            map(
                lambda m: m * np.ones(
                    days_per_month[m],
                    dtype=np.int64
                ),
                range(12)
            )
        )
    )

def year_2_day_index(ns):
    """ computes the index of the day at the end of the year n in ns
    this works on vectors 
    """
    return np.array(list(map(lambda n:days_per_year*n,ns)))

def day_2_year_index(ns):
    """ computes the index of the year
    this works on vectors 
    """
    return np.array(list(map(lambda i_d:int(days_per_year/i_d),ns)))


def month_2_day_index(ns):
    """ computes the index of the day at the end of the month n in ns
    this works on vectors and is faster than a recursive version working
    on a single index (since the smaller indices are handled anyway)
    """

    # We first compute the sequence of day indices up to the highest month in ns
    # and then select from this sequence the day indices for the months in ns
    days_per_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    dpm = (days_per_month[i % len(days_per_month)] for i in range(max(ns)))

    # compute indices for which we want to store the results which is the
    # list of partial sums of the above list  (repeated)

    def f(acc, el):
        if len(acc) < 1:
            res = (el,)
        else:
            last = acc[-1]
            res = acc + (el + last,)
        return res

    day_indices_for_continuous_moths = reduce(
        f,
        dpm,
        (0,)
    )
    day_indices = reduce(
        lambda acc, n: acc + [day_indices_for_continuous_moths[n]],  # for n=0 we want 0
        ns,
        []
    )
    return day_indices


class TimeStepIterator2():
    """iterator for looping forward over the results of a difference equation
    X_{i+1}=f(X_{i},i)"""

    def __init__(
            self,
            initial_values,  # a tuple of values that will be
            f,  # the function to compute the next ts
            max_it=False
    ):
        self.initial_values = initial_values
        self.f = f
        self.reset()
        self.max_it = max_it

    def reset(self):
        self.i = 0
        self.ts = self.initial_values

    def __iter__(self):
        self.reset()
        return self

    def __next__(self):
        if self.max_it:
            if self.i == self.max_it:
                raise StopIteration

        ts = copy(self.ts)
        ts_new = self.f(self.i, ts)
        self.ts = ts_new
        self.i += 1
        return ts

    def values(self, day_indices):
        # we traverse the iterator to the highest index and
        # collect the results we want to keep in a list (acc)
        tsi = copy(self)
        tsi.reset()

        def g(acc, i):
            v = tsi.__next__()
            if i in day_indices:
                acc += [v]
            return acc

        xs = reduce(g, range(max(day_indices) + 1), ([]))
        return xs


def respiration_from_compartmental_matrix(B, X):
    """This function computes the combined respiration from all pools"""
    return -np.sum(B @ X)


def plot_solutions(
        fig,
        times,
        var_names,
        tup,
        names=None
):
    if names is None:
        names = tuple(str(i) for i in range(len(tup)))

    #from IPython import embed; embed()
    assert (all([tup[0].shape == el.shape for el in tup]))

    if tup[0].ndim == 1:
        n_times = tup[0].shape[0]
        ax = fig.subplots(1, 1)
        for i, sol in enumerate(tup):
            ax.plot(
                np.array(times).reshape(n_times, ),
                sol,
                marker="o",
                label=names[i]
            )
            ax.set_title(var_names[0])
            ax.legend()
    else:
        n_times, n_vars = tup[0].shape

        fig.set_figheight(n_vars * fig.get_figwidth())
        axs = fig.subplots(n_vars, 1)
        colors = ('red', 'blue', 'green', 'orange')
        for j in range(n_vars):
            for i, sol in enumerate(tup):
                axs[j].plot(
                    np.array(times).reshape(n_times, ),
                    sol[:, j],
                    marker="+",
                    label=names[i]
                )
                axs[j].set_title(var_names[j])
                axs[j].legend()




def global_mean(lats,lons,arr):
    # assuming an equidistant grid.
    delta_lat=(lats.max()- lats.min())/(len(lats)-1)
    delta_lon=(lons.max() -lons.min())/(len(lons)-1)

    pixel_area = make_pixel_area_on_unit_spehre(delta_lat, delta_lon)
    
    #copy the mask from the array (first time step) 
    weight_mask=arr.mask[0,:,:] if  arr.mask.any() else False

    weight_mat= np.ma.array(
        np.array(
            [
                    [   
                        pixel_area(lats[lat_ind]) 
                        for lon_ind in range(len(lons))    
                    ]
                for lat_ind in range(len(lats))    
            ]
        ),
        mask = weight_mask 
    )
    
    # to compute the sum of weights we add only those weights that
    # do not correspond to an unmasked grid cell
    return  (weight_mat*arr).sum(axis=(1,2))/weight_mat.sum()
    



def grad2rad(alpha_in_grad):
    return np.pi/180*alpha_in_grad


def make_pixel_area_on_unit_spehre(delta_lat,delta_lon,sym=False):  
    # we compute the are of a delta_phi * delta_theta patch 
    # on the unit ball centered around phi,theta  
    # (which depends on theta but not
    # on phi)
    # the infinitesimal area element dA = sin(theta)*d_phi * d_theta
    # we have to integrate it from phi_min to phi_max
    # and from theta_min to theta_max
    if sym:
        # we can do this with sympy (for testing) 
        for v in ('theta','phi','theta_min', 'theta_max','phi_min','phi_max'):
            var(v)
        
        # We can do this symbolicaly with sympy just for testing...
        A_sym = integrate(
                    integrate(
                        sin(theta),
                        (theta,theta_min,theta_max)
                    ),
                    (phi,phi_min,phi_max)
        )
        # translate this to a numeric function
        A_num=lambdify((theta_min,theta_max,phi_min,phi_max),A_sym,modules=['numpy'])
    else:
        # or manually solve the integral since it is very simple
        def A_num(theta_min,theta_max,phi_min,phi_max):
            return (
                (phi_max-phi_min)
                *
                (-np.cos(theta_max) + np.cos(theta_min))
            )

    delta_theta, delta_phi = map(grad2rad, ( delta_lat, delta_lon))
    dth = delta_theta/2.0
    dph = delta_phi/2.0
    
    def A_patch(theta):
        # computes the area of a pixel on the unitsphere
        if np.abs(theta<dth/100): #(==0)  
            # pixel centered at north pole only extends northwards
            #print("##################### north pole ##########")
            theta_min_v=0.0
            theta_max_v=dth
        elif np.abs(theta > np.pi-dth/100): #==pi) 
            # pixel centered at south pole only extends northwards
            #print("##################### south pole ##########")
            theta_min_v=np.pi-dth
            theta_max_v=np.pi 
        else: 
            # normal pixel extends south and north-wards
            theta_min_v=theta-dth
            theta_max_v=theta+dth

        phi_min_v = -dph
        phi_max_v = +dph
        res = A_num(
            theta_min_v,
	    theta_max_v,
	    phi_min_v,
	    phi_max_v
        )
        #print(res)
        return res
     

    def pixel_area_on_unit_sphere(lat):
        # computes the fraction of the area of the sphere covered by this pixel
        theta_grad=lat+90
        theta = grad2rad(theta_grad)
        # the area of the unitsphere is 4 * pi
        return A_patch(theta)

    return pixel_area_on_unit_sphere

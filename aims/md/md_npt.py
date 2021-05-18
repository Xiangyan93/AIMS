#!/usr/bin/env python
# -*- coding: utf-8 -*-
from typing import Dict, Iterator, List, Optional, Union, Literal, Tuple
import os

CWD = os.path.dirname(os.path.abspath(__file__))
DIR_DATA = os.path.join(CWD, '..', '..', 'data')
import math
import json
from ..args import MonitorArgs
from ..database import *
from ..aimstools.simulator.gromacs import Npt
from ..aimstools.jobmanager import Slurm
from ..aimstools.utils import create_dir


def get_NptSimulator(args: MonitorArgs) -> Npt:
    return Npt(packmol_exe=args.packmol_exe, dff_root=args.dff_root, gmx_exe_analysis=args.gmx_exe_analysis,
               gmx_exe_mdrun=args.gmx_exe_mdrun)


def create(args: MonitorArgs):
    # create dir.
    create_dir(os.path.join(DIR_DATA, 'ms'))
    create_dir(os.path.join(DIR_DATA, 'slurm'))
    create_dir(os.path.join(DIR_DATA, 'tmp'))
    # crete jobs.
    for mol in session.query(Molecule).filter_by(active_learning=True):
        mol.create_md_npt()
    session.commit()


def build(args: MonitorArgs, simulator: Npt):
    for mol in _get_n_mols(args.n_prepare, eq_status=Status.STARTED):
        # create dirs.
        path = os.path.join(mol.ms_dir, 'md_npt', 'build')
        if not os.path.exists(path):
            os.mkdir(os.path.join(mol.ms_dir, 'md_npt'))
            os.mkdir(path)

        simulator.build(path=path, smiles_list=[mol.smiles], export=True)
        for job in mol.md_npt:
            job.status = Status.BUILD
        session.commit()

    for job in session.query(MD_NPT).filter_by(status=Status.BUILD):
        job.commands = json.dumps(
            simulator.prepare(path=job.ms_dir, n_jobs=args.n_hypercores, T=job.T, P=job.P, drde=True, T_basic=298)
        )
        job.status = Status.PREPARED
        session.commit()


def run(args: MonitorArgs, simulator: Npt, job_manager: Slurm):
    n_submit = args.n_run - job_manager.n_current_jobs
    n_jobs_per_mol = 56
    if n_submit > 0:
        for mol in _get_n_mols(math.ceil(n_submit * args.n_gmx_multi / n_jobs_per_mol), in_status=Status.PREPARED):
            jobs_to_run = []
            for job in mol.md_npt:
                if job.status == Status.PREPARED:
                    jobs_to_run.append(job)
            _submit_jobs(jobs_to_run=jobs_to_run,
                         simulator=simulator,
                         job_manager=job_manager,
                         n_gmx_multi=args.n_gmx_multi)


def analyze(args: MonitorArgs, simulator: Npt, job_manager: Slurm):
    for job in session.query(MD_NPT).filter_by(status=Status.SUBMITED).limit(args.n_analyze):
        if not job_manager.is_running(job.slurm_name):
            result = simulator.analyze(path=job.ms_dir)
            job.update_dict('result', result)
            if result.get('failed'):
                job.status = Status.FAILED
            elif result.get('continue'):
                job.status = Status.NOT_CONVERGED
            else:
                job.status = Status.ANALYZED
            session.commit()


def extend(args: MonitorArgs, simulator: Npt, job_manager: Slurm):
    jobs_to_run = session.query(MD_NPT).filter_by(status=Status.NOT_CONVERGED).all()
    if len(jobs_to_run) == 0:
        return

    for job in jobs_to_run:
        continue_n = json.loads(job.result).get('continue_n')
        assert continue_n is not None
        commands = simulator.extend(continue_n=continue_n, n_jobs=args.n_hypercores)
    _submit_jobs(jobs_to_run=jobs_to_run,
                 simulator=simulator,
                 job_manager=job_manager,
                 n_gmx_multi=args.n_gmx_multi,
                 extend=True,
                 commands=commands)


def _submit_jobs(jobs_to_run: List, simulator: Npt, job_manager: Slurm, n_gmx_multi: int,
                 extend: bool = False, commands: List[str] = None):
    if n_gmx_multi == 1:
        for job in jobs_to_run:
            name = job.name + '_extend' if extend else job.name
            sh = job_manager.generate_sh(path=job.ms_dir,
                                         name=name,
                                         commands=commands or json.loads(job.commands),
                                         sh_index=True)
            job_manager.submit(sh)
            job.update_list('sh_file', [sh])
            job.status = Status.SUBMITED
            session.commit()
    else:
        # make sure n_jobs % n_gmx_multi == 0
        if len(jobs_to_run) % n_gmx_multi != 0:
            jobs_to_run = jobs_to_run[:-len(jobs_to_run) % n_gmx_multi]
        #
        if job_manager.n_gpu != 0:
            assert n_gmx_multi % job_manager.n_gpu == 0

        jobs_list = [jobs_to_run[i * n_gmx_multi:(i + 1) * n_gmx_multi]
                     for i in range(int(len(jobs_to_run) / n_gmx_multi))]

        multi_cmds = json.loads(jobs_to_run[0].commands)
        multi_dirs = [job.ms_dir for job in jobs_to_run]
        commands_list = simulator.gmx.generate_gpu_multidir_cmds(multi_dirs, multi_cmds,
                                                                 n_parallel=n_gmx_multi,
                                                                 n_gpu=job_manager.n_gpu,
                                                                 n_omp=None)
        if extend:
            path = os.path.join(DIR_DATA, 'slurm')
            name = 'aims_md_npt_extend'
        else:
            mol = jobs_to_run[0].molecule
            for job in jobs_to_run:
                assert job.molecule.id == mol.id
            path = os.path.join(jobs_to_run[0].molecule.ms_dir, 'md_npt')
            name = 'aims_md_npt_ID%d' % mol.id
        for i, commands in enumerate(commands_list):
            sh = job_manager.generate_sh(path=path,
                                         name=name,
                                         commands=commands,
                                         n_gpu=job_manager.n_gpu,
                                         sh_index=True)
            job_manager.submit(sh)
            for job in jobs_list[i]:
                job.update_list('sh_file', [sh])
                job.status = Status.SUBMITED
            session.commit()


def _get_n_mols(n_mol: int, eq_status: int = None, in_status: int = None) -> List[Molecule]:
    mols = []
    for mol in session.query(Molecule).filter_by(active_learning=True):
        if mol.status_md_npt.__class__ == int:
            if mol.status_md_npt == eq_status:
                mols.append(mol)
            elif in_status is not None and mol.status_md_npt == in_status:
                mols.append(mol)
        elif mol.status_md_npt.__class__ == list:
            if in_status in mol.status_md_npt:
                mols.append(mol)

        if len(mols) == n_mol:
            return mols
    else:
        return mols

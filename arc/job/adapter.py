"""
A module for the abstract JobAdapter class

A job execution type could be either "incore" (run on the calling machine) or "queue" (submitted to the manager)
A job could (independently) be of a single process (e.g., optimizing a single well)
or a multi-process.
A single process job could run "incore" or be submitted the the queue.
A multi-process job could be executed "incore" (taking no advantage of potential server parallelization)
or be submitted to the queue, in which case a powerful job array will be automatically created.
"""

import csv
import datetime
import math
import os
import shutil
from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional, Tuple, Union

import pandas as pd

from arc.common import get_logger
from arc.exceptions import JobError
from arc.job.inputs import input_files
from arc.job.local import (get_last_modified_time,
                           delete_job,
                           execute_command,
                           check_job_status,
                           rename_output,
                           )
from arc.job.ssh import SSHClient
from arc.job.scans import generate_scan_points
from arc.job.submit import pipe_submit, submit_scripts
from arc.settings import (arc_path,
                          default_job_settings,
                          servers,
                          submit_filenames,
                          t_max_format,
                          output_filenames,
                          )
from arc.species.converter import xyz_to_str
from arc.job.trsh import determine_ess_status


logger = get_logger()

constraint_type_dict = {2: 'B', 3: 'A', 4: 'D'}


class JobEnum(str, Enum):
    """
    The supported job software adapters.
    The available adapters are a finite set.
    """
    arc = 'arc'
    cosmo = 'cosmo'
    gaussian = 'gaussian'
    molpro = 'molpro'
    onedmin = 'onedmin'
    orca = 'orca'
    psi4 = 'psi4'
    qchem = 'qchem'
    rdkit = 'rdkit'
    terachem = 'terachem'
    torchani = 'torchani'
    turbomol = 'turbomol'
    xtb = 'xtb'

    # TS search methods
    # Todo: see https://doi.org/10.1021/acs.jctc.7b00764
    autotst = 'autotst'  # AutoTST
    gsm = 'gsm'  # double ended growing string method (DE-GSM)
    pygsm = 'pygsm'  # double ended growing string method (DE-GSM)
    heuristics = 'heuristics'  # brute force heuristics
    kinbot = 'kinbot'  # KinBot
    gnn_isomerization = 'gnn_isomerization'  # Graph neural network for isomerization, https://doi.org/10.1021/acs.jpclett.0c00500
    neb_ase = 'neb_ase'  # NEB in ASE
    neb_terachem = 'neb_terachem'  # NEB in TeraChem
    qst2 = 'qst2'  # Synchronous Transit-Guided Quasi-Newton (STQN) implemented in Gaussian
    user = 'user'  # user guesses


class JobTypeEnum(str, Enum):
    """
    The supported job types.
    The available jon types are a finite set.
    """
    composite = 'composite'
    conformers = 'conformers'  # conformer optimization (not generation)
    freq = 'freq'
    gen_confs = 'gen_confs'  # conformer generation
    irc = 'irc'
    onedmin = 'onedmin'
    opt = 'opt'
    optfreq = 'optfreq'
    orbitals = 'orbitals'
    scan = 'scan'
    sp = 'sp'
    ts = 'ts'  # TS search


class JobExecutionTypeEnum(str, Enum):
    """
    The supported job execution types.
    The available job execution types are a finite set.
    """
    incore = 'incore'
    queue = 'queue'


class JobAdapter(ABC):
    """
    An abstract class for job adapters.
    """

    @abstractmethod
    def write_input_file(self) -> None:
        """
        Write the input file to execute the job on the server.
        """

    @abstractmethod
    def set_up_files(self) -> None:
        """
        Set files to be uploaded and downloaded. Writes the files if needed.
        Modifies the self.files_to_upload and self.files_to_download attributes.

        self.files_to_download is a list of remote paths.

        self.files_to_upload is a list of dictionaries, each with the following keys:
        ``'name'``, ``'source'``, ``'make_x'``, ``'local'``, and ``'remote'``.
        If ``'source'`` = ``'path'``, then the value in ``'local'`` is treated as a file path.
        Else if ``'source'`` = ``'input_files'``, then the value in ``'local'`` will be taken
        from the respective entry in inputs.py
        If ``'make_x'`` is ``True``, the file will be made executable.
        """
        pass

    @abstractmethod
    def set_additional_file_paths(self) -> None:
        """
        Set additional file paths specific for the adapter.
        Called from set_file_paths() and extends it.
        """
        pass

    @abstractmethod
    def set_input_file_memory(self) -> None:
        """
        Set the input_file_memory attribute.
        """
        # # determine amount of memory in job input file based on ESS
        # if self.job_adapter in ['molpro', 'terachem']:
        #     # Molpro's and TeraChem's memory is per cpu core and in MW (mega word; 1 MW ~= 8 MB; 1 GB = 128 MW)
        #     self.input_file_memory = math.ceil(self.job_memory_gb * 128 / self.cpu_cores)
        # elif self.job_adapter in ['orca']:
        #     # Orca's memory is per cpu core and in MB
        #     self.input_file_memory = math.ceil(self.job_memory_gb * 1024 / self.cpu_cores)
        # elif self.job_adapter in ['qchem']:
        #     # QChem manages its memory automatically, for now ARC will not intervene
        #     # see http://www.q-chem.com/qchem-website/manual/qchem44_manual/CCparallel.html
        #     self.input_file_memory = math.ceil(self.job_memory_gb)
        pass

    @abstractmethod
    def execute(self) -> Optional[dict]:
        """
        Execute a job.

        Returns: Optional[dict]
            Only returns a value if running incore.
        """
        pass

    def determine_job_array_parameters(self):
        """
        Determine the number of tasks to use in a job array
        and whether to iterate by conformers, species, reactions, or scan constraints.
        """
        if len(self.job_types) > 1:
            self.iterate_by.append('job_types')

        for job_type in self.job_types:

            if self.species is not None:
                if len(self.species) > 1:
                    self.iterate_by.append('species')
                for species in self.species:
                    if job_type == 'conformers':
                        self.iterate_by.append('conformers')
                        self.number_of_processes += len(species.conformers)
                    elif job_type == 'scan' and self.scan_type in ['brute_force_sp',
                                                                   'brute_force_opt',
                                                                   'cont_opt',
                                                                   'brute_force_sp_diagonal',
                                                                   'brute_force_opt_diagonal',
                                                                   'cont_opt_diagonal']:
                        self.iterate_by.append('scan')
                        scan_points_per_dimension = 360.0 / self.scan_res
                        for rotor_dict in species.rotors_dict.values():
                            if rotor_dict['directed_scan_type'] == 'ess':
                                self.number_of_processes += 1
                            elif 'cont_opt' in rotor_dict['directed_scan_type']:
                                # A single calculation per species for a continuous scan, either diagonal or not.
                                self.number_of_processes += 1
                            elif 'brute_force' in rotor_dict['directed_scan_type']:
                                if 'diagonal' in rotor_dict['directed_scan_type']:
                                    self.number_of_processes += scan_points_per_dimension
                                else:
                                    self.number_of_processes += \
                                        sum([scan_points_per_dimension ** len(rotor_dict['scan'])])

            elif self.reactions is not None and len(self.reactions) > 1:
                self.iterate_by.append('reactions')
                self.number_of_processes += len(self.reactions)

        if self.number_of_processes > 1:
            if self.tasks is None:
                # A trend line for the desired number of nodes vs. number of processes: y = 1.7 x ^ 0.35
                # gives the following output: 10 -> 4, 100 -> 9, 1000 -> 20, 1e4 -> 43, 1e5 -> 96.
                # Cap the number of tasks at 100.
                # Use just 1 or 2 tasks if there are less than 10 processes.
                self.tasks = 1 if self.number_of_processes <= 2 \
                    else 2 if self.number_of_processes < 10 \
                    else min(math.ceil(1.7 * self.number_of_processes ** 0.35), 100)
            self.write_hdf5()

    def write_hdf5(self):
        """
        Write the HDF5 data file for job arrays.
        Each data point is a dictionary representation of the DataPoint class.
        Note: each data point always runs "incore". A job array is created once the pipe is submitted to the queue
        (rather than running the pipe incore, taking no advantage of the server's potential for parallelization).
        """
        if self.iterate_by:
            data = dict()
            if 'reactions' in self.iterate_by:
                for reaction in self.reactions:
                    data[reaction.index].append(DataPoint(charge=reaction.charge,
                                                          job_types=[self.job_type],
                                                          label=reaction.label,
                                                          level=self.level.as_dict(),
                                                          multiplicity=reaction.multiplicity,
                                                          xyz_1=reaction.get_reactants_xyz(),
                                                          xyz_2=reaction.get_products_xyz(),
                                                          constraints=self.constraints,
                                                          ).as_dict())
            else:
                for species in self.species:
                    data[species.label] = list()
                    if 'conformers' in self.iterate_by:
                        for conformer in species.conformers:
                            data[species.label].append(DataPoint(charge=species.charge,
                                                                 job_types=['opt'],
                                                                 label=species.label,
                                                                 level=self.level.as_dict(),
                                                                 multiplicity=species.multiplicity,
                                                                 xyz_1=conformer,
                                                                 ).as_dict())
                    elif 'scan' in self.iterate_by:
                        data[species.label] = generate_scan_points(species=species, scan_res=self.scan_res)
                    elif 'species' in self.iterate_by:
                        data[species.label].append(DataPoint(charge=species.charge,
                                                             job_types=[self.job_type],
                                                             label=species.label,
                                                             level=self.level.as_dict(),
                                                             multiplicity=species.multiplicity,
                                                             xyz_1=species.get_xyz(),
                                                             constraints=self.constraints,
                                                             ).as_dict())

            df = pd.json_normalize(data)
            df.to_hdf(os.path.join(self.local_path, 'data.hdf5'), key='df', mode='w')

    def write_submit_script(self) -> None:
        """
        Write a submit script to execute the job.
        """
        if self.max_job_time > 9999 or self.max_job_time <= 0:
            self.max_job_time = 120
        architecture = ''
        if self.server.lower() == 'pharos':
            # here we're hard-coding ARC for Pharos, a Green Group server
            # If your server has different node architectures, implement something similar
            architecture = '\n#$ -l harpertown' if self.cpu_cores <= 8 else '\n#$ -l magnycours'

        submit_script = submit_scripts[self.server][self.job_adapter] if self.tasks is None \
            else pipe_submit[self.server]
        try:
            submit_script = submit_script.format(
                name=self.job_name,
                un=servers[self.server]['un'],
                t_max=self.format_max_job_time(time_format=t_max_format[servers[self.server]['cluster_soft']]),
                memory=int(self.submit_script_memory),
                cpus=self.cpu_cores,
                architecture=architecture,
                max_task_num=self.tasks,
                arc_path=arc_path,
                hdf5_path=os.path.join(self.remote_path, 'data.hdf5'),
            )
        except KeyError:
            if self.tasks is None:
                submit_scripts_for_printing = {server: [software for software in values.keys()]
                                               for server, values in submit_scripts.items()}
                pipe = ''
            else:
                submit_scripts_for_printing = {server for server, values in pipe_submit.keys()}
                pipe = ' pipe'
            logger.error(f'Could not find{pipe} submit script for server {self.server} and software {self.job_adapter}. '
                         f'Make sure your submit scripts (under arc/job/submit.py) are updated with the servers '
                         f'and software defined in arc/settings.py\n'
                         f'Alternatively, It is possible that you defined parameters in curly braces (e.g., {{PARAM}}) '
                         f'in your submit script/s. To avoid error, replace them with double curly braces (e.g., '
                         f'{{{{PARAM}}}} instead of {{PARAM}}.\nIdentified the following submit scripts:\n'
                         f'{submit_scripts_for_printing}')
            raise

        with open(os.path.join(self.local_path, submit_filenames[servers[self.server]['cluster_soft']]), 'w') as f:
            f.write(submit_script)

    def set_file_paths(self):
        """
        Set local and remote job file paths.
        """
        folder_name = 'TS_guesses' if self.reactions is not None else 'TSs' if self.species[0].is_ts else 'Species'
        self.local_path = os.path.join(self.project_directory, 'calcs', folder_name, self.species_label, self.job_name)
        self.local_path_to_output_file = os.path.join(self.local_path, 'output.out')

        self.local_path_to_orbitals_file = os.path.join(self.local_path, 'orbitals.fchk')  # todo: qchem
        self.local_path_to_lj_file = os.path.join(self.local_path, 'lj.dat')  # Todo: onedmin
        self.local_path_to_hess_file = os.path.join(self.local_path, 'input.hess')
        self.local_path_to_xyz = None  # Todo: only used for Terachem, perhaps move to that adapter

        if not os.path.isdir(self.local_path):
            os.makedirs(self.local_path)

        # parentheses don't play well in folder names:
        species_name_remote = self.species_label.replace('(', '_').replace(')', '_')
        self.remote_path = os.path.join('runs', 'ARC_Projects', self.project, species_name_remote, self.job_name)

        self.set_additional_file_paths()

    def MOVE_TO_ADAPTERS(self):
        """Todo: TMP method"""
        self.additional_files_to_upload = list()
        # self.additional_files_to_upload is a list of dictionaries, each with the following keys:
        # 'name', 'source', 'make_x', 'local', and 'remote'.
        # If 'source' = 'path', then the value in 'local' is treated as a file path.
        # If 'source' = 'input_files', then the value in 'local' will be taken from the respective entry in inputs.py
        # If 'make_x' is True, the file will be made executable.
        if self.job_type == 'onedmin':
            with open(os.path.join(self.local_path, 'geo.xyz'), 'w') as f:
                f.write(xyz_to_str(self.species.get_xyz()))
            self.additional_files_to_upload.append({'name': 'geo',
                                                    'source': 'path',
                                                    'make_x': False,
                                                    'local': os.path.join(self.local_path, 'geo.xyz'),
                                                    'remote': os.path.join(self.remote_path, 'geo.xyz')})
            # make the m.x file executable
            self.additional_files_to_upload.append({'name': 'm.x',
                                                    'source': 'input_files',
                                                    'make_x': True,
                                                    'local': 'onedmin.molpro.x',
                                                    'remote': os.path.join(self.remote_path, 'm.x')})
            self.additional_files_to_upload.append({'name': 'qc.mol', 'source': 'input_files', 'make_x': False,
                                                    'local': 'onedmin.qc.mol',
                                                    'remote': os.path.join(self.remote_path, 'qc.mol')})
        if self.job_adapter == 'terachem':
            self.additional_files_to_upload.append({'name': 'geo',
                                                    'source': 'path',
                                                    'make_x': False,
                                                    'local': os.path.join(self.local_path, 'coord.xyz'),
                                                    'remote': os.path.join(self.remote_path, 'coord.xyz')})

    def upload_files(self):
        """
        Upload the relevant files for the job.
        """
        if self.execution_type != 'incore' and self.server != 'local':
            # If the job execution type is incore, then no need to upload any files.
            # Also, even if the job is submitted to the que, no need to upload files if the server is local.
            with SSHClient(self.server) as ssh:
                for up_file in self.files_to_upload:
                    logger.debug(f"Uploading {up_file['name']} source {up_file['source']} to {self.server}")
                    if up_file['source'] == 'path':
                        ssh.upload_file(remote_file_path=up_file['remote'], local_file_path=up_file['local'])
                    elif up_file['source'] == 'input_files':
                        ssh.upload_file(remote_file_path=up_file['remote'], file_string=input_files[up_file['local']])
                    else:
                        raise ValueError(f"Unclear file source for {up_file['name']}. Should either be 'path' or "
                                         f"'input_files', got: {up_file['source']}")
                    if up_file['make_x']:
                        ssh.change_mode(mode='+x', file_name=up_file['name'], remote_path=self.remote_path)
        else:
            # running locally, just copy the check file, if exists, to the job folder
            for up_file in self.files_to_upload:
                if up_file['name'] == 'checkfile':
                    shutil.copyfile(src=up_file['local'], dst=os.path.join(self.local_path, 'check.chk'))
        self.initial_time = datetime.datetime.now()

    def download_files(self):
        """
        Download the relevant files.
        """
        if self.execution_type != 'incore' and self.server != 'local':
            # If the job execution type is incore, then no need to download any files.
            # Also, even if the job is submitted to the que, no need to download files if the server is local.
            with SSHClient(self.server) as ssh:
                for dl_file in self.files_to_download:
                    ssh.download_file(remote_file_path=dl_file['remote'], local_file_path=dl_file['local'])
                if dl_file['file_name'] == output_filenames[self.job_adapter]:
                    self.final_time = ssh.get_last_modified_time(remote_file_path=dl_file['local'])
        elif self.server == 'local':
            self.final_time = get_last_modified_time(
                file_path=os.path.join(self.local_path, output_filenames[self.job_adapter]))
        self.final_time = self.final_time or datetime.datetime.now()

    def determine_run_time(self):
        """
        Determine the run time. Update self.run_time and round to seconds.
        """
        if self.initial_time is not None and self.final_time is not None:
            time_delta = self.final_time - self.initial_time
            remainder = time_delta.microseconds > 5e5
            self.run_time = datetime.timedelta(seconds=time_delta.seconds + remainder)
        else:
            self.run_time = None

    def _set_job_number(self):
        """
        Used as the entry number in the database, as well as the job name on the server.
        This is not an abstract method and should not be overwritten.
        """
        csv_path = os.path.join(arc_path, 'initiated_jobs.csv')
        if os.path.isfile(csv_path):
            # check that this is the updated version
            with open(csv_path, 'r') as f:
                d_reader = csv.DictReader(f)
                headers = d_reader.fieldnames
            if 'comments' in headers:
                os.remove(csv_path)
        if not os.path.isfile(csv_path):
            with open(csv_path, 'w') as f:
                writer = csv.writer(f, dialect='excel')
                row = ['job_num', 'project', 'species_label', 'job_type', 'is_ts', 'charge', 'multiplicity',
                       'job_name', 'job_id', 'server', 'job_adapter', 'memory', 'level']
                writer.writerow(row)
        with open(csv_path, 'r') as f:
            reader = csv.reader(f, dialect='excel')
            job_num = 0
            for _ in reader:
                job_num += 1
                if job_num == 100000:
                    job_num = 0
            self.job_num = job_num

    def _write_initiated_job_to_csv_file(self):
        """
        Write an initiated ARC job into the initiated_jobs.csv file.
        """
        if not self.testing:
            csv_path = os.path.join(arc_path, 'initiated_jobs.csv')
            with open(csv_path, 'a') as f:
                writer = csv.writer(f, dialect='excel')
                row = [self.job_num, self.project, self.species_label, self.job_type, self.is_ts, self.charge,
                       self.multiplicity, self.job_name, self.job_id, self.server, self.job_adapter,
                       self.job_memory_gb, str(self.level)]
                writer.writerow(row)

    def write_completed_job_to_csv_file(self):
        """
        Write a completed ARCJob into the completed_jobs.csv file.
        """
        if not self.testing:
            if self.job_status[0] != 'done' or self.job_status[1]['status'] != 'done':
                self.determine_job_status()
            csv_path = os.path.join(arc_path, 'completed_jobs.csv')
            if os.path.isfile(csv_path):
                # check that this is the updated version
                with open(csv_path, 'r') as f:
                    d_reader = csv.DictReader(f)
                headers = d_reader.fieldnames
                if 'comments' in headers:
                    os.remove(csv_path)
            if not os.path.isfile(csv_path):
                # check file, make index file and write headers if file doesn't exists
                with open(csv_path, 'w') as f:
                    writer = csv.writer(f, dialect='excel')
                    row = ['job_num', 'project', 'species_label', 'job_type', 'is_ts', 'charge', 'multiplicity',
                           'job_name', 'job_id', 'server', 'job_adapter', 'memory', 'level', 'initial_time',
                           'final_time', 'run_time', 'job_status_(server)', 'job_status_(ESS)',
                           'ESS troubleshooting methods used']
                    writer.writerow(row)
            csv_path = os.path.join(arc_path, 'completed_jobs.csv')
            with open(csv_path, 'a') as f:
                writer = csv.writer(f, dialect='excel')
                job_type = self.job_type
                if self.fine:
                    job_type += ' (fine)'
                row = [self.job_num, self.project, self.species_label, job_type, self.is_ts, self.charge,
                       self.multiplicity, self.job_name, self.job_id, self.server, self.job_adapter,
                       self.job_memory_gb, str(self.level), self.initial_time, self.final_time,
                       self.run_time, self.job_status[0], self.job_status[1]['status'], self.ess_trsh_methods]
                writer.writerow(row)

    def set_cpu_and_mem(self):
        """
        Set cpu and memory based on ESS and cluster software.
        This is not an abstract method and should not be overwritten.
        """
        max_cpu = servers[self.server].get('cpus', None)  # max cpus per node on server
        # set to 8 if user did not specify cpu in settings and in ARC input file
        job_cpu_cores = default_job_settings.get('job_cpu_cores', 8)
        if max_cpu is not None and job_cpu_cores > max_cpu:
            job_cpu_cores = max_cpu
        self.cpu_cores = self.cpu_cores or job_cpu_cores

        max_mem = servers[self.server].get('memory', None)  # max memory per node in GB
        job_max_server_node_memory_allocation = default_job_settings.get('job_max_server_node_memory_allocation', 0.8)
        if max_mem is not None and self.job_memory_gb > max_mem * job_max_server_node_memory_allocation:
            logger.warning(f'The memory for job {self.job_name} using {self.job_adapter} ({self.job_memory_gb} GB) '
                           f'exceeds {100 * job_max_server_node_memory_allocation}% of the the maximum node memory on '
                           f'{self.server}. Setting it to {job_max_server_node_memory_allocation * max_mem:.2f} GB.')
            self.job_memory_gb = job_max_server_node_memory_allocation * max_mem
            total_submit_script_memory = self.job_memory_gb * 1024 * 1.05  # MB
            self.job_status[1]['keywords'].append('max_total_job_memory')  # useful info when trouble shoot
        else:
            total_submit_script_memory = self.job_memory_gb * 1024 * 1.1  # MB

        # determine amount of memory in submit script based on cluster job scheduling system
        cluster_software = servers[self.server].get('cluster_soft').lower()
        if cluster_software in ['oge', 'sge']:
            # In SGE, "-l h_vmem=5000M" specifies the amount of maximum memory required for all cores to be 5000 MB.
            self.submit_script_memory = math.ceil(total_submit_script_memory)  # in MB
        elif cluster_software in ['slurm']:
            # In Slurm, "#SBATCH --mem-per-cpu=2000" specifies the amount of memory required per cpu core to be 2000 MB.
            self.submit_script_memory = math.ceil(total_submit_script_memory / self.cpu_cores)  # in MB

        self.set_input_file_memory()

    def as_dict(self) -> dict:
        """
        A helper function for dumping this object as a dictionary, used for saving in the restart file.
        """
        job_dict = dict()
        job_dict['job_adapter'] = self.job_adapter
        job_dict['execution'] = self.execution
        job_dict['job_type'] = self.job_type
        job_dict['level'] = self.level
        job_dict['project'] = self.project
        job_dict['project_directory'] = self.project_directory
        job_dict['args'] = self.args
        if self.bath_gas is not None:
            job_dict['bath_gas'] = self.bath_gas
        if self.checkfile is not None:
            job_dict['checkfile'] = self.checkfile
        if self.conformers:
            job_dict['conformers'] = self.conformers
        job_dict['cpu_cores'] = self.cpu_cores
        job_dict['ess_settings'] = self.ess_settings
        if self.ess_trsh_methods:
            job_dict['ess_trsh_methods'] = self.ess_trsh_methods
        if self.fine:
            job_dict['fine'] = self.fine
        if self.final_time is not None:
            job_dict['final_time'] = self.final_time.strftime('%Y-%m-%d %H:%M:%S')
        if self.initial_time is not None:
            job_dict['initial_time'] = self.initial_time.strftime('%Y-%m-%d %H:%M:%S')
        job_dict['job_id'] = self.job_id
        job_dict['job_memory_gb'] = float(self.job_memory_gb)
        job_dict['job_name'] = self.job_name
        job_dict['job_num'] = self.job_num
        job_dict['job_status'] = self.job_status
        job_dict['max_job_time'] = self.max_job_time
        if self.reactions is not None:
            job_dict['reaction_indices'] = [reaction.index for reaction in self.reactions]
        if self.rotor_index is not None:
            job_dict['rotor_index'] = self.rotor_index
        if self.scan is not None:
            job_dict['scan'] = self.scan
        if self.scan_type is not None:
            job_dict['scan_type'] = self.scan_type
        if self.server_nodes:
            job_dict['server_nodes'] = self.server_nodes
        if self.species is not None:
            job_dict['species_labels'] = [species.label for species in self.species]
        return job_dict

    def format_max_job_time(self, time_format: str) -> str:
        """
        Convert the max_job_time attribute into a format supported by the server cluster software.

        Args:
            time_format (str): Either ``'days'`` (e.g., 5-0:00:00) or ``'hours'`` (e.g., 120:00:00).

        Returns: str
            The formatted maximum job time string.
        """
        t_delta = datetime.timedelta(hours=self.max_job_time)
        if time_format == 'days':
            # e.g., 5-0:00:00
            t_max = f'{t_delta.days}-{datetime.timedelta(seconds=t_delta.seconds)}'
        elif time_format == 'hours':
            # e.g., 120:00:00
            h, s = divmod(t_delta.seconds, 3600)
            h += t_delta.days * 24
            t_max = f"{h}:{':'.join(str(datetime.timedelta(seconds=s)).split(':')[1:])}"
        else:
            raise JobError(f"Could not determine a format for maximal job time.\n Format is determined by "
                           f"{t_max_format}, but got {servers[self.server]['cluster_soft']} for {self.server}")
        return t_max

    def delete(self):
        """
        Delete a running Job.
        """
        logger.debug(f'Deleting job {self.job_name} for {self.species_label}')
        if self.server != 'local':
            logger.debug(f'deleting job on {self.server}...')
            with SSHClient(self.server) as ssh:
                ssh.delete_job(self.job_id)
        else:
            logger.debug('deleting job locally...')
            delete_job(job_id=self.job_id)

    def determine_job_status(self):
        """
        Determine the Job's status. Updates self.job_status.

        Raises:
            IOError: If the output file and any additional server information cannot be found.
        """
        if self.job_status[0] == 'errored':
            return
        self.job_status[0] = self._check_job_server_status()
        if self.job_status[0] == 'done':
            try:
                self._check_job_ess_status()  # populates self.job_status[1], and downloads the output file
            except IOError:
                logger.error(f'Got an IOError when trying to download output file for job {self.job_name}.')
                content = self._get_additional_job_info()
                if content:
                    logger.info('Got the following information from the server:')
                    logger.info(content)
                    for line in content.splitlines():
                        # example:
                        # slurmstepd: *** JOB 7752164 CANCELLED AT 2019-03-27T00:30:50 DUE TO TIME LIMIT on node096 ***
                        if 'cancelled' in line.lower() and 'due to time limit' in line.lower():
                            logger.warning(f'Looks like the job was cancelled on {self.server} due to time limit. '
                                           f'Got: {line}')
                            new_max_job_time = self.max_job_time - 24 if self.max_job_time > 25 else 1
                            logger.warning(f'Setting max job time to {new_max_job_time} (was {self.max_job_time})')
                            self.max_job_time = new_max_job_time
                            self.job_status[1]['status'] = 'errored'
                            self.job_status[1]['keywords'] = ['ServerTimeLimit']
                            self.job_status[1]['error'] = 'Job cancelled by the server since it reached the maximal ' \
                                                          'time limit.'
                            self.job_status[1]['line'] = ''
                raise
        elif self.job_status[0] == 'running':
            self.job_status[1]['status'] = 'running'

    def _get_additional_job_info(self):
        """
        Download the additional information of stdout and stderr from the server.
        """
        lines1, lines2 = list(), list()
        content = ''
        cluster_soft = servers[self.server]['cluster_soft'].lower()
        if cluster_soft in ['oge', 'sge']:
            local_file_path1 = os.path.join(self.local_path, 'out.txt')
            local_file_path2 = os.path.join(self.local_path, 'err.txt')
            if self.server != 'local':
                remote_file_path = os.path.join(self.remote_path, 'out.txt')
                with SSHClient(self.server) as ssh:
                    try:
                        ssh.download_file(remote_file_path=remote_file_path,
                                          local_file_path=local_file_path1)
                    except (TypeError, IOError) as e:
                        logger.warning(f'Got the following error when trying to download out.txt for {self.job_name}:')
                        logger.warning(e)
                    remote_file_path = os.path.join(self.remote_path, 'err.txt')
                    try:
                        ssh.download_file(remote_file_path=remote_file_path, local_file_path=local_file_path2)
                    except (TypeError, IOError) as e:
                        logger.warning(f'Got the following error when trying to download err.txt for {self.job_name}:')
                        logger.warning(e)
            if os.path.isfile(local_file_path1):
                with open(local_file_path1, 'r') as f:
                    lines1 = f.readlines()
            if os.path.isfile(local_file_path2):
                with open(local_file_path2, 'r') as f:
                    lines2 = f.readlines()
            content += ''.join([line for line in lines1])
            content += '\n'
            content += ''.join([line for line in lines2])
        elif cluster_soft == 'slurm':
            if self.server != 'local':
                with SSHClient(self.server) as ssh:
                    response = ssh.list_dir(remote_path=self.remote_path)
            else:
                response = execute_command('ls -alF {0}'.format(self.local_path))
            files = list()
            for line in response[0]:
                files.append(line.split()[-1])
            for file_name in files:
                if 'slurm' in file_name and '.out' in file_name:
                    local_file_path = os.path.join(self.local_path, file_name)
                    if self.server != 'local':
                        remote_file_path = os.path.join(self.remote_path, file_name)
                        try:
                            with SSHClient(self.server) as ssh:
                                ssh.download_file(remote_file_path=remote_file_path,
                                                  local_file_path=local_file_path)
                        except (TypeError, IOError) as e:
                            logger.warning(f'Got the following error when trying to download {file_name} '
                                           f'for {self.job_name}: {e}')
                    if os.path.isfile(local_file_path):
                        with open(local_file_path, 'r') as f:
                            lines1 = f.readlines()
                    content += ''.join([line for line in lines1])
                    content += '\n'
        return content

    def _check_job_server_status(self):
        """
        Possible statuses: `initializing`, `running`, `errored on node xx`, `done`.
        """
        if self.server != 'local':
            with SSHClient(self.server) as ssh:
                return ssh.check_job_status(self.job_id)
        else:
            return check_job_status(self.job_id)

    def _check_job_ess_status(self):
        """
        Check the status of the job ran by the electronic structure software (ESS).
        Possible statuses: `initializing`, `running`, `errored: {error type / message}`, `unconverged`, `done`.
        """
        if self.server != 'local':
            if os.path.exists(self.local_path_to_output_file):
                os.remove(self.local_path_to_output_file)
            if os.path.exists(self.local_path_to_orbitals_file):
                os.remove(self.local_path_to_orbitals_file)
            if os.path.exists(self.local_path_to_check_file):
                os.remove(self.local_path_to_check_file)
            self._download_output_file()  # also downloads the check file and orbital file if exist
        else:
            # If running locally, just rename the output file to "output.out" for consistency between software
            if self.final_time is None:
                self.final_time = get_last_modified_time(
                    file_path=os.path.join(self.local_path, output_filenames[self.software]))
            rename_output(local_file_path=self.local_path_to_output_file, software=self.software)
            xyz_path = os.path.join(self.local_path, 'scr', 'optim.xyz')
            if os.path.isfile(xyz_path):
                self.local_path_to_xyz = xyz_path
        self.determine_run_time()
        status, keywords, error, line = determine_ess_status(output_path=self.local_path_to_output_file,
                                                             species_label=self.species_label,
                                                             job_type=self.job_type,
                                                             software=self.software)
        self.job_status[1]['status'] = status
        self.job_status[1]['keywords'] = keywords
        self.job_status[1]['error'] = error
        self.job_status[1]['line'] = line.rstrip()

    def add_to_args(self,
                    val: str,
                    key1: str = 'keyword',
                    key2: str = 'general',
                    separator: Optional[str] = None,
                    check_val_exists: bool = True,
                    ):
        """
        Add arguments to self.args in a nested dictionary under self.args[key1][key2].

        Args:
            val (str): The value to add.
            key1 (str, optional): Key1.
            key2 (str, optional): Key2.
            separator (str, optional): A separator (e.g., ``' '``  or ``'\\n'``)
                                       to apply between existing values and new values.
            check_val_exists (bool, optional): Only append ``val`` if it doesn't exist in the dictionary.
        """
        separator = separator if separator is not None else '\n\n' if key1 == 'block' else ' '
        if key1 not in list(self.args.keys()):
            self.args[key1] = dict()
        val_exists = self.args[key1] and key2 in self.args[key1] and val in self.args[key1][key2]
        if not (check_val_exists and val_exists):
            separator = separator if key2 in list(self.args[key1].keys()) else ''
            self.args[key1][key2] = val if key2 not in self.args[key1] else self.args[key1][key2] + separator + val

    def _log_job_execution(self):
        """Log executing this job"""
        info = ''
        if self.fine:
            info += ' (fine opt)'
        if self.job_type == 'scan':
            pivots = [scan[1:3] for scan in self.scan] if len(self.scan) > 1 else self.scan[0][1:3]
            constraints = list()
            for constraint_tuple in self.constraints:
                constraints.append(f'{constraint_type_dict[len(constraint_tuple[0])]} '
                                   f'{constraint_tuple[0]} {constraint_tuple[1]}:.2f')
            constraints = constraints[0] if len(constraints) == 1 else constraints
            info += f'(pivots: {pivots}'
            info += f' constraints: {constraints})' if constraints else ')'
        execution_type = {'array': 'job array', 'incore': 'incore job', 'single': 'job'}[self.execution_type]
        logger.info(f'Running {execution_type} {self.job_name} for {self.species_label}{info}')

    def get_file_property_dictionary(self,
                                     file_name: str,
                                     local: str = '',
                                     remote: str = '',
                                     source: str = 'path',
                                     make_x: bool = False,
                                     ):
        """
        Get a dictionary that represents a file to be uploaded or downloaded to/from a server via SSH.

        Args:
            file_name (str): The file name.
            local (str, optional): The full local path.
            remote (str, optional): The full remote path.
            source (str, optional): Either ``'path'`` to treat the ``'local'`` attribute as a file path,
                                    or ``'input_files'`` to take the respective entry from inputs.py.
            make_x (bool, optional): Whether to make the file executable, default: ``False``.
        """
        if not file_name:
            raise ValueError('file_name cannot be empty')
        if source not in ['path', 'input_files']:
            raise ValueError(f'The source argument must be either "path" or "input_files", got {source}.')
        local = local or os.path.join(self.local_path, file_name)
        remote = remote or os.path.join(self.remote_path, file_name)
        return {'file_name': file_name,
                'local': local,
                'remote': remote,
                'source': source,
                'make_x': make_x,
                }


class DataPoint(object):
    """
    A class for representing a data point dictionary (a single job) per species for the HDF5 file.

    Args:
        job_types (List[str]): The job types to be executed in sequence.
        label (str): The species label.
        level (dict): The level of theory, a Level.dict() representation.
        xyz_1 (dict): The cartesian coordinates to consider.
        args (dict, str, optional): Methods (including troubleshooting) to be used in input files.
        bath_gas (str, optional): A bath gas. Currently only used in OneDMin to calculate L-J parameters.
        charge (int): The species (or TS) charge.
        constraints (List[Tuple[List[int], float]], optional): Optimization constraint.
        cpu_cores (int, optional): The total number of cpu cores requested for a job.
        fine (bool, optional): Whether to use fine geometry optimization parameters. Default: ``False``.
        irc_direction (str, optional): The direction of the IRC job (`forward` or `reverse`).
        multiplicity (int): The species (or TS) multiplicity.
        xyz_2 (dict, optional): Additional cartesian coordinates to consider in double-ended TS search methods.
    """

    def __init__(self,
                 job_types: List[str],
                 label: str,
                 level: dict,
                 xyz_1: dict,
                 args: Optional[Union[dict, str]] = None,
                 bath_gas: Optional[str] = None,
                 charge: int = 0,
                 constraints: Optional[List[Tuple[List[int], float]]] = None,
                 cpu_cores: Optional[str] = None,
                 fine: bool = False,
                 irc_direction: Optional[str] = None,
                 multiplicity: int = 1,
                 xyz_2: Optional[dict] = None,
                 ):
        self.job_types = job_types
        self.label = label
        self.level = level
        self.xyz_1 = xyz_1

        self.args = args
        self.bath_gas = bath_gas
        self.charge = charge
        self.constraints = constraints
        self.cpu_cores = cpu_cores
        self.fine = fine
        self.irc_direction = irc_direction
        self.multiplicity = multiplicity
        self.xyz_2 = xyz_2

        self.status = 0

        # initialize outputs
        self.electronic_energy = None
        self.error = None
        self.frequencies = None
        self.xyz_out = None

    def as_dict(self):
        """
        A dictionary representation of the object, not storing default or trivial data.

        Returns: dict
            The dictionary representation.
        """
        result = {'job_types': self.job_types,
                  'label': self.label,
                  'level': self.level,
                  'xyz_1': self.xyz_1,
                  'status': self.status,
                  'electronic_energy': self.electronic_energy,
                  'error': self.error,
                  'frequencies': self.frequencies,
                  'xyz_out': self.xyz_out,
                  }
        if self.args is not None:
            result['args'] = self.args
        if self.bath_gas is not None:
            result['bath_gas'] = self.bath_gas
        if self.charge != 0:
            result['charge'] = self.charge
        if self.constraints is not None:
            result['constraints'] = self.constraints
        if self.cpu_cores is not None:
            result['cpu_cores'] = self.cpu_cores
        if self.fine:
            result['fine'] = self.fine
        if self.irc_direction is not None:
            result['irc_direction'] = self.irc_direction
        if self.multiplicity != 1:
            result['multiplicity'] = self.multiplicity
        if self.xyz_2 is not None:
            result['xyz_2'] = self.xyz_2
        return result
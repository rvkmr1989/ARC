#!/usr/bin/env python3
# encoding: utf-8

"""
This module contains unit tests of the arc.job.trsh module
"""

import os
import unittest

import arc.job.trsh as trsh
from arc.settings import arc_path, supported_ess


class TestTrsh(unittest.TestCase):
    """
    Contains unit tests for the job.trsh module
    """

    @classmethod
    def setUpClass(cls):
        """
        A method that is run before all unit tests in this class.
        """
        cls.maxDiff = None
        path = os.path.join(arc_path, 'arc', 'testing', 'trsh')
        cls.base_path = {ess: os.path.join(path, ess) for ess in supported_ess}

    def test_determine_ess_status(self):
        """Test the determine_ess_status() function"""

        # Gaussian

        path = os.path.join(self.base_path['gaussian'], 'converged.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='OH', job_type='opt')
        self.assertEqual(status, 'done')
        self.assertEqual(keywords, list())
        self.assertEqual(error, '')
        self.assertEqual(line, '')

        path = os.path.join(self.base_path['gaussian'], 'l913.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='tst', job_type='composite')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['MaxOptCycles', 'GL913'])
        self.assertEqual(error, 'Maximum optimization cycles reached.')
        self.assertIn('Error termination via Lnk1e', line)
        self.assertIn('g09/l913.exe', line)

        path = os.path.join(self.base_path['gaussian'], 'l301.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='Zr2O4H', job_type='opt')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['GL301', 'BasisSet'])
        self.assertEqual(error, 'The basis set 6-311G is not appropriate for the this chemistry.')
        self.assertIn('Error termination via Lnk1e', line)
        self.assertIn('g16/l301.exe', line)

        path = os.path.join(self.base_path['gaussian'], 'l9999.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='Zr2O4H', job_type='opt')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Unconverged', 'GL9999'])
        self.assertEqual(error, 'Unconverged')
        self.assertIn('Error termination via Lnk1e', line)
        self.assertIn('g16/l9999.exe', line)

        path = os.path.join(self.base_path['gaussian'], 'syntax.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='Zr2O4H', job_type='opt')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Syntax'])
        self.assertEqual(error, 'There was a syntax error in the Gaussian input file. Check your Gaussian input '
                                'file template under arc/job/inputs.py. Alternatively, perhaps the level of theory '
                                'is not supported by Gaussian in the format it was given.')
        self.assertFalse(line)

        # QChem

        path = os.path.join(self.base_path['qchem'], 'H2_opt.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='H2', job_type='opt')
        self.assertEqual(status, 'done')
        self.assertEqual(keywords, list())
        self.assertEqual(error, '')
        self.assertEqual(line, '')

        # Molpro

        path = os.path.join(self.base_path['molpro'], 'unrecognized_basis_set.out')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='I', job_type='sp')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['BasisSet'])
        self.assertEqual(error, 'Unrecognized basis set 6-311G**')
        self.assertIn(' ? Basis library exhausted', line)  # line includes '\n'

        # Orca

        # test detection of a successful job
        path = os.path.join(self.base_path['orca'], 'orca_successful_sp.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'done')
        self.assertEqual(keywords, list())
        self.assertEqual(error, '')
        self.assertEqual(line, '')

        # test detection of SCF energy diverge issue
        path = os.path.join(self.base_path['orca'], 'orca_scf_blow_up_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['SCF'])
        expected_error_msg = 'The SCF energy seems diverged during iterations. ' \
                             'SCF energy after initial iteration is -1076.6615662471. ' \
                             'SCF energy after final iteration is -20006124.68383977. ' \
                             'The ratio between final and initial SCF energy is 18581.627979509627. ' \
                             'This ratio is greater than the default threshold of 2. ' \
                             'Please consider using alternative methods or larger basis sets.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('', line)

        # test detection of insufficient memory causes SCF failure
        path = os.path.join(self.base_path['orca'], 'orca_scf_memory_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['SCF', 'Memory'])
        expected_error_msg = 'Orca suggests to increase per cpu core memory to 289 MB.'
        self.assertEqual(error, expected_error_msg)
        self.assertEqual(' Error  (ORCA_SCF): Not enough memory available!', line)

        # test detection of insufficient memory causes MDCI failure
        path = os.path.join(self.base_path['orca'], 'orca_mdci_memory_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['MDCI', 'Memory'])
        expected_error_msg = 'Orca suggests to increase per cpu core memory to 9718 MB.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('Please increase MaxCore', line)

        # test detection of too many cpu cores causes MDCI failure
        path = os.path.join(self.base_path['orca'], 'orca_too_many_cores.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['MDCI', 'cpu'])
        expected_error_msg = 'Orca cannot utilize cpu cores more than electron pairs in a molecule. ' \
                             'The maximum number of cpu cores can be used for this job is 10.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('parallel calculation exceeds', line)

        # test detection of generic MDCI failure
        path = os.path.join(self.base_path['orca'], 'orca_mdci_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['MDCI'])
        expected_error_msg = 'MDCI error in Orca.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('ORCA finished by error termination in MDCI', line)

        # test detection of multiplicty and charge combination error
        path = os.path.join(self.base_path['orca'], 'orca_multiplicity_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Input'])
        expected_error_msg = 'The multiplicity and charge combination for species test are wrong.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('Error : multiplicity', line)

        # test detection of input keyword error
        path = os.path.join(self.base_path['orca'], 'orca_input_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Syntax'])
        expected_error_msg = 'There was keyword syntax error in the Orca input file. In particular, keywords ' \
                             'XTB1 can either be duplicated or illegal. Please check your Orca ' \
                             'input file template under arc/job/inputs.py. Alternatively, perhaps the level of ' \
                             'theory or the job option is not supported by Orca in the format it was given.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('XTB1', line)

        # test detection of basis set error (e.g., input contains elements not supported by specified basis)
        path = os.path.join(self.base_path['orca'], 'orca_basis_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Basis'])
        expected_error_msg = 'There was a basis set error in the Orca input file. In particular, basis for atom type ' \
                             'Br is missing. Please check if specified basis set supports this atom.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('There are no CABS', line)

        # test detection of wavefunction convergence failure
        path = os.path.join(self.base_path['orca'], 'orca_wavefunction_not_converge_error.log')
        status, keywords, error, line = trsh.determine_ess_status(
            output_path=path, species_label='test', job_type='sp', software='orca')
        self.assertEqual(status, 'errored')
        self.assertEqual(keywords, ['Convergence'])
        expected_error_msg = 'Specified wavefunction method is not converged. Please restart calculation with larger ' \
                             'max iterations or with different convergence flags.'
        self.assertEqual(error, expected_error_msg)
        self.assertIn('This wavefunction IS NOT FULLY CONVERGED!', line)

    def test_trsh_ess_job(self):
        """Test the trsh_ess_job() function"""

        # test gaussian
        label = 'ethanol'
        level_of_theory_dict = {'method': 'ccsd', 'basis': 'vdz'}
        server = 'server1'
        job_type = 'opt'
        software = 'gaussian'
        fine = False
        memory_gb = 16
        num_heavy_atoms = 2
        ess_trsh_methods = ['change_node', 'int=(Acc2E=14)']
        cpu_cores = 8

        # gaussian: test 1
        job_status = {'keywords': ['CheckFile']}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)

        self.assertTrue(remove_checkfile)
        self.assertEqual(software, 'gaussian')
        self.assertEqual(memory, 16)
        self.assertFalse(couldnt_trsh)

        # gaussian: test 2
        job_status = {'keywords': ['InternalCoordinateError']}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)

        self.assertFalse(remove_checkfile)
        self.assertEqual(trsh_keyword, 'opt=(cartesian,nosymm)')

        # gaussian: test 3
        job_status = {'keywords': ['tmp']}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)

        self.assertIn('cbs-qb3', ess_trsh_methods)
        self.assertEqual(level_of_theory_dict['method'], 'cbs-qb3')
        self.assertEqual(job_type, 'composite')

        # test qchem
        software = 'qchem'
        ess_trsh_methods = ['change_node']
        job_status = {'keywords': ['MaxOptCycles', 'Unconverged']}
        # qchem: test 1
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('max_cycles', ess_trsh_methods)

        # qchem: test 2
        job_status = {'keywords': ['SCF']}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('DIIS_GDM', ess_trsh_methods)

        # test molpro
        software = 'molpro'

        # molpro: test
        path = os.path.join(self.base_path['molpro'], 'insufficient_memory.out')
        label = 'TS'
        level_of_theory_dict = {'method': 'mrci', 'basis': 'aug-cc-pV(T+d)Z'}
        server = 'server1'
        status, keywords, error, line = trsh.determine_ess_status(output_path=path, species_label='TS', job_type='sp')
        job_status = {'keywords': keywords, 'error': error}
        job_type = 'sp'
        fine = True
        memory_gb = 32.0
        ess_trsh_methods = ['change_node']
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('memory', ess_trsh_methods)
        self.assertAlmostEqual(memory, 222.15625)

        # test orca
        # orca: test 1
        # Test troubleshooting insufficient memory issue
        # Automatically increase memory provided not exceeding maximum available memory
        label = 'test'
        level_of_theory_dict = {'method': 'dlpno-ccsd(T)'}
        server = 'server1'
        job_type = 'sp'
        software = 'orca'
        fine = True
        memory_gb = 250
        cpu_cores = 32
        num_heavy_atoms = 20
        ess_trsh_methods = ['memory']
        path = os.path.join(self.base_path['orca'], 'orca_mdci_memory_error.log')
        status, keywords, error, line = trsh.determine_ess_status(output_path=path, species_label='TS', job_type='sp',
                                                                  software=software)
        job_status = {'keywords': keywords, 'error': error}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('memory', ess_trsh_methods)
        self.assertEqual(cpu_cores, 32)
        self.assertAlmostEqual(memory, 312)

        # orca: test 2
        # Test troubleshooting insufficient memory issue
        # Automatically reduce cpu cores to ensure enough memory per core when maximum available memory is limited
        label = 'test'
        level_of_theory_dict = {'method': 'dlpno-ccsd(T)'}
        server = 'server1'
        job_type = 'sp'
        software = 'orca'
        fine = True
        memory_gb = 250
        cpu_cores = 32
        num_heavy_atoms = 20
        ess_trsh_methods = ['memory']
        path = os.path.join(self.base_path['orca'], 'orca_mdci_memory_error.log')
        status, keywords, error, line = trsh.determine_ess_status(output_path=path, species_label='TS', job_type='sp',
                                                                  software=software)
        keywords.append('max_total_job_memory')
        job_status = {'keywords': keywords, 'error': error}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('memory', ess_trsh_methods)
        self.assertEqual(cpu_cores, 24)
        self.assertAlmostEqual(memory, 235)

        # orca: test 3
        # Test troubleshooting insufficient memory issue
        # Stop troubleshooting when ARC determined there is not enough computational resource to accomplish the job
        label = 'test'
        level_of_theory_dict = {'method': 'dlpno-ccsd(T)'}
        server = 'server1'
        job_type = 'sp'
        software = 'orca'
        fine = True
        memory_gb = 1
        cpu_cores = 32
        num_heavy_atoms = 20
        ess_trsh_methods = ['memory']
        path = os.path.join(self.base_path['orca'], 'orca_mdci_memory_error.log')
        status, keywords, error, line = trsh.determine_ess_status(output_path=path, species_label='TS', job_type='sp',
                                                                  software=software)
        keywords.append('max_total_job_memory')
        job_status = {'keywords': keywords, 'error': error}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('memory', ess_trsh_methods)
        self.assertEqual(couldnt_trsh, True)
        self.assertLess(cpu_cores, 1)  # can't really run job with less than 1 cpu ^o^

        # orca: test 4
        # Test troubleshooting too many cpu cores
        # Automatically reduce cpu cores
        label = 'test'
        level_of_theory_dict = {'method': 'dlpno-ccsd(T)'}
        server = 'server1'
        job_type = 'sp'
        software = 'orca'
        fine = True
        memory_gb = 16
        cpu_cores = 16
        num_heavy_atoms = 1
        ess_trsh_methods = ['cpu']
        path = os.path.join(self.base_path['orca'], 'orca_too_many_cores.log')
        status, keywords, error, line = trsh.determine_ess_status(output_path=path, species_label='TS', job_type='sp',
                                                                  software=software)
        job_status = {'keywords': keywords, 'error': error}
        output_errors, ess_trsh_methods, remove_checkfile, level_of_theory_dict, software, job_type, fine, trsh_keyword, \
        memory, shift, cpu_cores, couldnt_trsh = trsh.trsh_ess_job(label, level_of_theory_dict, server, job_status,
                                                                   job_type,
                                                                   software, fine, memory_gb, num_heavy_atoms,
                                                                   cpu_cores, ess_trsh_methods)
        self.assertIn('cpu', ess_trsh_methods)
        self.assertEqual(cpu_cores, 10)

    def test_trsh_negative_freq(self):
        """Test troubleshooting a negative frequency"""
        gaussian_neg_freq_path = os.path.join(arc_path, 'arc', 'testing', 'Gaussian_neg_freq.out')
        current_neg_freqs_trshed, conformers, output_errors, output_warnings = \
            trsh.trsh_negative_freq(label='2-methoxy_n-methylaniline', log_file=gaussian_neg_freq_path)
        expected_current_neg_freqs_trshed = [-18.07]
        self.assertEqual(current_neg_freqs_trshed, expected_current_neg_freqs_trshed)
        expected_conformers = [{'symbols': ('C', 'N', 'C', 'C', 'O', 'C', 'C', 'C', 'C', 'C',
                                            'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H'),
                                'isotopes': (12, 14, 12, 12, 16, 12, 12, 12, 12, 12, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
                                'coords': ((0.400778, 2.986759, -1.212666), (1.11819, 1.945985, -0.541264),
                                           (1.261692, 2.029477, 0.877185), (1.979036, 0.991891, 1.507776),
                                           (2.542752, -0.092156, 0.821642),
                                           (2.536206, -0.416927, -0.032073999999999936), (2.135627, 1.068178, 2.880226),
                                           (1.619002, 2.117912, 3.706318), (0.917097, 3.141999, 3.1800360000000003),
                                           (0.746925, 3.095369, 1.8246509999999998),
                                           (0.960395, 3.9472880000000004, -1.027618),
                                           (-0.7235929999999999, 3.096434, -0.80703), (0.377648, 2.76298, -2.377599),
                                           (2.699509, 0.318782, -0.480887), (3.085125, -1.355514, 0.03929700000000008),
                                           (1.852124, -0.510783, -0.26326), (2.680608, 0.26868, 3.287566),
                                           (1.758456, 2.146776, 4.779234), (0.510864, 3.967211, 3.8494729999999997),
                                           (0.203748, 3.890122, 1.410319))},
                               {'symbols': ('C', 'N', 'C', 'C', 'O', 'C', 'C', 'C', 'C', 'C',
                                            'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H', 'H'),
                                'isotopes': (12, 14, 12, 12, 16, 12, 12, 12, 12, 12, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
                                'coords': ((0.400778, 2.986759, -1.014666), (1.11819, 1.945985, -0.321264),
                                           (1.261692, 2.029477, 0.899185), (1.979036, 0.991891, 1.661776),
                                           (2.542752, -0.092156, 1.261642), (2.536206, -0.416927, -0.648074),
                                           (2.135627, 1.068178, 3.056226), (1.619002, 2.117912, 3.6843179999999998),
                                           (0.917097, 3.141999, 2.916036), (0.746925, 3.095369, 1.560651),
                                           (0.7843950000000001, 3.991288, -0.939618),
                                           (-0.547593, 3.0524340000000003, -0.71903), (0.377648, 2.76298, -1.981599),
                                           (3.359509, 0.38478199999999996, -1.382887), (3.085125, -1.355514, -0.840703),
                                           (1.192124, -0.576783, -1.16526), (2.680608, 0.26868, 3.617566),
                                           (1.758456, 2.146776, 4.7572339999999995), (0.510864, 3.967211, 3.387473),
                                           (0.203748, 3.890122, 0.992319))}]
        self.assertEqual(conformers, expected_conformers)
        self.assertEqual(output_errors, list())
        self.assertEqual(output_warnings, list())


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2))
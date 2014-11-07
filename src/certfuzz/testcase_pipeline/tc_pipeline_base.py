'''
Created on Jul 16, 2014

@organization: cert.org
'''
import Queue
import abc
import logging

from certfuzz.analyzers.errors import AnalyzerEmptyOutputError
from certfuzz.file_handlers.watchdog_file import touch_watchdog_file
from certfuzz.helpers.coroutine import coroutine
from certfuzz.testcase_pipeline.errors import TestCasePipelineError
import os
from certfuzz.fuzztools import filetools
import shutil


logger = logging.getLogger(__name__)


class TestCasePipelineBase(object):
    '''
    Implements a pipeline for filtering and processing a testcase
    '''
    __metaclass__ = abc.ABCMeta

    def __init__(self, testcases=None, uniq_func=None, cfg=None, options=None,
                 outdir=None, workdirbase=None):
        '''
        Constructor
        '''
        self.cfg = cfg
        self.options = options
        self.uniq_func = uniq_func
        self.outdir = outdir

        self.working_dir = workdirbase

        self.tc_candidate_q = Queue.Queue()

        self.analyzer_classes = []
        self._setup_analyzers()

        # this gets set up in __enter__
        self.analysis_pipeline = None

        if testcases is not None:
            for testcase in testcases:
                self.tc_candidate_q.put(testcase)

    def __enter__(self):
        self._setup_analysis_pipeline()
        return self.go

    def __exit__(self, etype, value, traceback):
        pass

    @abc.abstractmethod
    def _setup_analyzers(self):
        pass

    def _setup_analysis_pipeline(self):
        # build up the pipeline:
        # verify | minimize | analyze | report
        r = self.report()
        a = self.analyze(r)
        m = self.minimize(a)

        self.analysis_pipeline = self.verify(m)

    @coroutine
    def verify(self, *targets):
        '''
        Verifies that a test case is unique before sending the test case. Acts
        as a filter on the analysis pipeline.
        :param targets: one or more downstream coroutines to send the testcase to
        '''
        logger.debug('Verifier standing by for testcases')
        while True:
            testcase = (yield)

            logger.debug('verify testcase')
            self._pre_verify(testcase)
            self._verify(testcase)
            self._post_verify(testcase)

            for target in targets:
                if testcase.should_proceed_with_analysis:
                    # we're ready to proceed with this testcase
                    # so send it downstream
                    target.send(testcase)

    @coroutine
    def minimize(self, *targets):
        logger.debug('Minimizer standing by for testcases')
        while True:
            testcase = (yield)

            logger.debug('minimize testcase')
            self._pre_minimize(testcase)
            self._minimize(testcase)
            self._post_minimize(testcase)

            for target in targets:
                target.send(testcase)

    @coroutine
    def analyze(self, *targets):
        '''
        Analyzes a test case before passing it down the pipeline
        :param targets: one or more downstream coroutines to send the testcase to
        '''
        logger.debug('Analyzer standing by for testcases')
        while True:
            testcase = (yield)

            logger.debug('analyze testcase')
            self._pre_analyze(testcase)
            self._analyze(testcase)
            self._post_analyze(testcase)

            for target in targets:
                target.send(testcase)

    @coroutine
    def report(self, *targets):
        '''
        Prepares the test case report.
        :param targets: one or more downstream coroutines to send the testcase to
        '''
        logger.debug('Reporter standing by for testcases')
        while True:
            testcase = (yield)

            logger.debug('report testcase')
            self._pre_report(testcase)
            self._report(testcase)
            self._post_report(testcase)

            for target in targets:
                target.send(testcase)

    def _pre_verify(self, testcase):
        pass

    @abc.abstractmethod
    def _verify(self, testcase):
        pass

    def _post_verify(self, testcase):
        pass

    def _pre_minimize(self, testcase):
        pass

    @abc.abstractmethod
    def _minimize(self, testcase):
        '''
        try to reduce the Hamming Distance between the testcase file and the
        known good seedfile. testcase.fuzzedfile will be replaced with the
        minimized result

        :param testcase: the testcase to work on
        '''

    def _post_minimize(self, testcase):
        pass

    def _pre_analyze(self, testcase):
        pass

    @abc.abstractmethod
    def _analyze(self, testcase):
        '''
        Loops through all known analyzer_classes for a given testcase
        :param testcase:
        '''
        for analyzer_class in self.analyzer_classes:
            touch_watchdog_file()

            analyzer_instance = analyzer_class(self.cfg, testcase)
            if analyzer_instance:
                try:
                    analyzer_instance.go()
                except AnalyzerEmptyOutputError:
                    logger.warning('Unexpected empty output from analyzer_class. Continuing')

    def _post_analyze(self, testcase):
        pass

    def _pre_report(self, testcase):
        pass

    @abc.abstractmethod
    def _report(self, testcase):
        pass

    def _post_report(self, testcase):
        pass

    def go(self):
        while not self.tc_candidate_q.empty():
            testcase = self.tc_candidate_q.get()
            self.analysis_pipeline.send(testcase)

    def _copy_files(self, testcase):
        if not self.outdir:
            raise TestCasePipelineError('No outdir set')

        logger.debug('target_base=%s', self.outdir)

        target_dir = testcase.result_dir

        if os.path.exists(target_dir):
            logger.debug('Repeat crash, will not copy to %s', target_dir)
            return

        # make sure target_base exists already
        filetools.find_or_create_dir(self.outdir)
        logger.debug('Copying to %s', target_dir)
        shutil.copytree(testcase.tempdir, target_dir)

        if not os.path.exists(target_dir):
            raise TestCasePipelineError('Failed to create target dir %s', target_dir)

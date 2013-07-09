#    Paperwork - Using OCR to grep dead trees the easy way
#    Copyright (C) 2012  Jerome Flesch
#
#    Paperwork is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Paperwork is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Paperwork.  If not, see <http://www.gnu.org/licenses/>.
"""
Contains all the code relative to keyword and document list management list.
Also everything related to indexation and searching in the documents (+
suggestions)
"""

import logging
import copy
import datetime
import multiprocessing
import os.path
import time
import threading

from gi.repository import GObject

import numpy
from sklearn.externals import joblib
from sklearn.linear_model.passive_aggressive import PassiveAggressiveClassifier

import whoosh.fields
import whoosh.index
import whoosh.qparser
import whoosh.query
from whoosh.query import Term
from whoosh import sorting

from paperwork.backend import img
from paperwork.backend.common.doc import BasicDoc
from paperwork.backend.img.doc import ImgDoc
from paperwork.backend.img.doc import is_img_doc
from paperwork.backend.pdf.doc import PdfDoc
from paperwork.backend.pdf.doc import is_pdf_doc
from paperwork.util import dummy_progress_cb
from paperwork.util import MIN_KEYWORD_LEN
from paperwork.util import mkdir_p
from paperwork.util import rm_rf
from paperwork.util import strip_accents

logger = logging.getLogger(__name__)

DOC_TYPE_LIST = [
    (is_pdf_doc, PdfDoc.doctype, PdfDoc),
    (is_img_doc, ImgDoc.doctype, ImgDoc)
]


class DummyDocSearch(object):
    """
    Dummy doc search object.

    Instantiating a DocSearch object takes time (the time to rereard the index).
    So you can use this object instead during this time as a placeholder
    """
    docs = []
    label_list = []

    def __init__(self):
        pass

    @staticmethod
    def get_doc_examiner():
        """ Do nothing """
        assert()

    @staticmethod
    def get_index_updater():
        """ Do nothing """
        assert()

    @staticmethod
    def find_suggestions(sentence):
        """ Do nothing """
        sentence = sentence  # to make pylint happy
        return []

    @staticmethod
    def find_documents(sentence):
        """ Do nothing """
        sentence = sentence  # to make pylint happy
        return []

    @staticmethod
    def add_label(label):
        """ Do nothing """
        label = label  # to make pylint happy
        assert()

    @staticmethod
    def redo_ocr(langs, progress_callback):
        """ Do nothing """
        # to make pylint happy
        langs = langs
        progress_callback = progress_callback
        assert()

    @staticmethod
    def update_label(old_label, new_label, cb_progress=None):
        """ Do nothing """
        # to make pylint happy
        old_label = old_label
        new_label = new_label
        cb_progress = cb_progress
        assert()

    @staticmethod
    def destroy_label(label, cb_progress=None):
        """ Do nothing """
        # to make pylint happy
        label = label
        cb_progress = cb_progress
        assert()

    @staticmethod
    def destroy_index():
        """ Do nothing """
        assert()

    @staticmethod
    def is_hash_in_index(filehash=None):
        """ Do nothing """
        assert()

class DocDirExaminer(GObject.GObject):
    """
    Examine a directory containing documents. It looks for new documents,
    modified documents, or deleted documents.
    """
    def __init__(self, docsearch):
        GObject.GObject.__init__(self)
        self.docsearch = docsearch
        # we may be run in an independent thread --> use an independent
        # searcher
        self.__searcher = docsearch.index.searcher()

    def examine_rootdir(self,
                        on_new_doc,
                        on_doc_modified,
                        on_doc_deleted,
                        progress_cb=dummy_progress_cb):
        """
        Examine the rootdir.
        Calls on_new_doc(doc), on_doc_modified(doc), on_doc_deleted(docid)
        every time a new, modified, or deleted document is found
        """
        # getting the doc list from the index
        query = whoosh.query.Every()
        results = self.__searcher.search(query, limit=None)
        old_doc_list = [result['docid'] for result in results]
        old_doc_infos = {}
        for result in results:
            old_doc_infos[result['docid']] = (result['doctype'],
                                              result['last_read'])
        old_doc_list = set(old_doc_list)

        # and compare it to the current directory content
        docdirs = os.listdir(self.docsearch.rootdir)
        progress = 0
        for docdir in docdirs:
            old_infos = old_doc_infos.get(docdir)
            doctype = None
            if old_infos is not None:
                doctype = old_infos[0]
            doc = self.docsearch.get_doc_from_docid(docdir, doctype)
            if doc is None:
                continue
            if docdir in old_doc_list:
                old_doc_list.remove(docdir)
                assert(old_infos is not None)
                last_mod = datetime.datetime.fromtimestamp(doc.last_mod)
                if old_infos[1] != last_mod:
                    on_doc_modified(doc)
            else:
                on_new_doc(doc)
            progress_cb(progress, len(docdirs),
                        DocSearch.INDEX_STEP_CHECKING, doc)
            progress += 1

        # remove all documents from the index that don't exist anymore
        for old_doc in old_doc_list:
            on_doc_deleted(old_doc)

        progress_cb(1, 1, DocSearch.INDEX_STEP_CHECKING)


class DocIndexUpdater(GObject.GObject):
    """
    Update the index content.
    Don't forget to call commit() to apply the changes
    """
    def __init__(self, docsearch, optimize, progress_cb=dummy_progress_cb):
        self.docsearch = docsearch
        self.optimize = optimize
        self.writer = docsearch.index.writer()
        self.progress_cb = progress_cb
        self.__need_reload = False

    def _update_doc_in_index(self, index_writer, doc,
                             fit_label_estimator=True):
        """
        Add/Update a document in the index
        """
        if fit_label_estimator:
            self.docsearch.fit_label_estimator(
                [doc], labels=self.docsearch.label_list + doc.labels)
        last_mod = datetime.datetime.fromtimestamp(doc.last_mod)
        docid = unicode(doc.docid)
        label = doc.get_index_labels()
        index_writer.update_document(
            docid=docid,
            doctype=doc.doctype,
            docfilehash=unicode(doc.get_docfilehash(), "utf-8"),
            content=doc.get_index_text(),
            label=doc.get_index_labels(),
            date=doc.date,
            last_read=last_mod
        )
        return True

    @staticmethod
    def _delete_doc_from_index(index_writer, docid):
        """
        Remove a document from the index
        """
        query = whoosh.query.Term("docid", docid)
        index_writer.delete_by_query(query)

    def add_doc(self, doc, fit_label_estimator=True):
        """
        Add a document to the index
        """
        logger.info("Indexing new doc: %s" % doc)
        self._update_doc_in_index(self.writer, doc,
                                  fit_label_estimator=fit_label_estimator)
        self.__need_reload = True

    def upd_doc(self, doc, fit_label_estimator=True):
        """
        Update a document in the index
        """
        logger.info("Updating modified doc: %s" % doc)
        self._update_doc_in_index(self.writer, doc,
                                  fit_label_estimator=fit_label_estimator)

    def del_doc(self, docid, fit_label_estimator=True):
        """
        Delete a document
        argument fit_label_estimator is not used but is needed for the
        same interface as upd_doc and add_doc
        """
        logger.info("Removing doc from the index: %s" % docid)
        self._delete_doc_from_index(self.writer, docid)
        self.__need_reload = True

    def commit(self):
        """
        Apply the changes to the index
        """
        logger.info("Index: Commiting changes and saving estimators")
        self.docsearch.save_label_estimators()
        self.writer.commit(optimize=self.optimize)
        del self.writer
        self.docsearch.reload_searcher()
        if self.__need_reload:
            logger.info("Index: Reloading ...")
            self.docsearch.reload_index(progress_cb=self.progress_cb)

    def cancel(self):
        """
        Forget about the changes
        """
        logger.info("Index: Index update cancelled")
        self.writer.cancel()
        del self.writer


def is_dir_empty(dirpath):
    """
    Check if the specified directory is empty or not
    """
    if not os.path.isdir(dirpath):
        return False
    return (len(os.listdir(dirpath)) <= 0)


class DocSearch(object):
    """
    Index a set of documents. Can provide:
        * documents that match a list of keywords
        * suggestions for user input.
        * instances of documents
    """

    INDEX_STEP_LOADING = "loading"
    INDEX_STEP_CLEANING = "cleaning"
    INDEX_STEP_CHECKING = "checking"
    INDEX_STEP_READING = "checking"
    INDEX_STEP_COMMIT = "commit"
    LABEL_STEP_UPDATING = "label updating"
    LABEL_STEP_DESTROYING = "label deletion"
    OCR_THREADS_POLLING_TIME = 0.5
    WHOOSH_SCHEMA = whoosh.fields.Schema( #static up to date schema
                docid=whoosh.fields.ID(stored=True, unique=True),
                doctype=whoosh.fields.ID(stored=True, unique=False),
                docfilehash=whoosh.fields.ID(stored=True),
                content=whoosh.fields.TEXT(spelling=True),
                label=whoosh.fields.KEYWORD(stored=True, commas=True,
                                            spelling=True, scorable=True),
                date=whoosh.fields.DATETIME(stored=True),
                last_read=whoosh.fields.DATETIME(stored=True),
            )
    LABEL_ESTIMATOR_TEMPLATE = PassiveAggressiveClassifier(n_iter=50)

    """
    Label_estimators is a dict with one estimator per label.
    Each label is predicted with its own estimator (OneVsAll strategy)
    We cannot use directly OneVsAllClassifier sklearn class because
    it doesn't support online learning (partial_fit)
    """
    label_estimators = {}

    def __init__(self, rootdir, callback=dummy_progress_cb):
        """
        Index files in rootdir (see constructor)

        Arguments:
            callback --- called during the indexation (may be called *often*).
                step : DocSearch.INDEX_STEP_READING or
                    DocSearch.INDEX_STEP_SORTING
                progression : how many elements done yet
                total : number of elements to do
                document (only if step == DocSearch.INDEX_STEP_READING): file
                    being read
        """
        self.rootdir = rootdir
        base_indexdir = os.getenv("XDG_DATA_HOME",
                                  os.path.expanduser("~/.local/share"))
        self.indexdir = os.path.join(base_indexdir, "paperwork", "index")
        mkdir_p(self.indexdir)

        self.__docs_by_id = {}  # docid --> doc
        self.label_list = []

        need_index_rewrite = True
        try:
            logger.info("Opening index dir '%s' ..." % self.indexdir)
            self.index = whoosh.index.open_dir(self.indexdir)
            # check that the schema is up-to-date
            # We use the string representation of the schemas, because previous
            # versions of whoosh don't always implement __eq__
            if str(self.index.schema) == str(self.WHOOSH_SCHEMA):
                need_index_rewrite = False
        except whoosh.index.EmptyIndexError, exc:
            logger.warning("Failed to open index '%s'" % self.indexdir)
            logger.warning("Exception was: %s" % str(exc))

        if need_index_rewrite:
            logger.info("Creating a new index")
            self.index = whoosh.index.create_in(self.indexdir,
                                                self.WHOOSH_SCHEMA)
            logger.info("Index '%s' created" % self.indexdir)

        self.__searcher = self.index.searcher()

        self.search_param_list = []

        class CustomFuzzy(whoosh.qparser.query.FuzzyTerm):
            def __init__(self, fieldname, text, boost=1.0, maxdist=1,
                 prefixlength=0, constantscore=True):
                whoosh.qparser.query.FuzzyTerm.__init__(self, fieldname, text, boost, maxdist,
                 prefixlength, constantscore=True)

        facets = [sorting.ScoreFacet(),sorting.FieldFacet("date", reverse=True)]
        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("label",
                                            schema=self.index.schema,
                                            termclass=Term),
                                       "sortedby" : facets})
        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("label",
                                            schema=self.index.schema,
                                            termclass=whoosh.qparser.query.Prefix),
                                       "sortedby" : facets})
        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("label",
                                            schema=self.index.schema,
                                            termclass=CustomFuzzy),
                                       "sortedby" : facets})

        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("content",
                                            schema=self.index.schema,
                                            termclass=Term),
                                       "sortedby" : facets})
        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("content",
                                            schema=self.index.schema,
                                            termclass=CustomFuzzy),
                                       "sortedby" : facets})
        self.search_param_list.append({"query_parser" : whoosh.qparser.QueryParser("content",
                                            schema=self.index.schema,
                                            termclass=whoosh.qparser.query.Prefix),
                                       "sortedby" : facets})

        self.check_workdir()
        self.cleanup_rootdir(callback)
        self.reload_index(callback)

        self.label_estimators_dir = os.path.join(base_indexdir,
                                                  "paperwork",
                                                  "label_estimators")
        self.label_estimators_file = os.path.join(self.label_estimators_dir,
                                                  "label_estimators.jbl")
        try:
            logger.info("Opening label_estimators file '%s' ..." %
                        self.label_estimators_file)
            (l_estimators,ver) = joblib.load(self.label_estimators_file)
            if ver != BasicDoc.FEATURES_VER:
                logger.info("Estimator version is not up to date")
                self.label_estimators = {}
            else:
                self.label_estimators = l_estimators

            # check that the label_estimators are up to date for their class
            for label_name in self.label_estimators:
                params = self.label_estimators[label_name].get_params()
                if params != self.LABEL_ESTIMATOR_TEMPLATE.get_params():
                    raise IndexError('label_estimators params are not up to date')
        except Exception, exc:
            logger.error("Failed to open label_estimator file '%s', or bad label_estimator structure"
                   % self.indexdir)
            logger.error("Exception was: %s" % exc)
            logger.info("Will create new label_estimators")
            self.label_estimators = {}

    def save_label_estimators(self):
        if not os.path.exists(self.label_estimators_dir):
            os.mkdir(self.label_estimators_dir)
        joblib.dump((self.label_estimators, BasicDoc.FEATURES_VER),
                    self.label_estimators_file,
                    compress=0)

    def __must_clean(self, filepath):
        must_clean_cbs = [
            is_dir_empty,
        ]
        for must_clean_cb in must_clean_cbs:
            if must_clean_cb(filepath):
                return True
        return False

    def check_workdir(self):
        """
        Check that the current work dir (see config.PaperworkConfig) exists. If
        not, open the settings dialog.
        """
        mkdir_p(self.rootdir)

    def cleanup_rootdir(self, progress_cb=dummy_progress_cb):
        """
        Remove all the crap from the work dir (temporary files, empty
        directories, etc)
        """
        progress_cb(0, 1, self.INDEX_STEP_CLEANING)
        for filename in os.listdir(self.rootdir):
            filepath = os.path.join(self.rootdir, filename)
            if self.__must_clean(filepath):
                logger.info("Cleanup: Removing '%s'" % filepath)
                rm_rf(filepath)
            elif os.path.isdir(filepath):
                # we only want to go one subdirectory deep, no more
                for subfilename in os.listdir(filepath):
                    subfilepath = os.path.join(filepath, subfilename)
                    if self.__must_clean(subfilepath):
                        logger.info("Cleanup: Removing '%s'" % subfilepath)
                        rm_rf(subfilepath)
        progress_cb(1, 1, self.INDEX_STEP_CLEANING)

    def get_doc_examiner(self):
        """
        Return an object useful to find added/modified/removed documents
        """
        return DocDirExaminer(self)

    def get_index_updater(self, optimize=True):
        """
        Return an object useful to update the content of the index

        Note that this object is only about modifying the index. It is not
        made to modify the documents themselves.
        Some helper methods, with more specific goals, may be available for
        what you want to do.
        """
        return DocIndexUpdater(self, optimize)

    def fit_label_estimator(self, docs=None, removed_label=None, labels=None):
        """
        fit the estimator with the supervised documents

        Arguments:
            docs --- a collection of documents to fit the estimator with
                if none, all the docs are used
            removed_label --- if the fitting is done when a label is removed
                a doc with no label is not used for learning (fitting), unless
                the label has been explicitely removed
            labels --- a collection a labels to operate with. If none, all
                the labels are used
        """
        if docs is None:
            docs = self.docs

        if labels is None:
            labels = []
            for doc in docs:
                labels += doc.labels

        label_name_set = set([label.name for label in labels])

        # construct the estimators if not present in the list
        for label_name in label_name_set:
            if label_name not in self.label_estimators:
                self.label_estimators[label_name] = copy.deepcopy(DocSearch.LABEL_ESTIMATOR_TEMPLATE)

        for doc in docs:
            logger.info("Fitting estimator with doc: %s " % doc)
            # fit only with labelled documents
            if doc.labels:
                for label_name in label_name_set:
                    # check for this estimator if the document is labelled or not
                    doc_has_label = 'unlabelled'
                    for label in doc.labels:
                        if label.name == label_name:
                            doc_has_label = 'labelled'
                            break

                    # fit the estimators with the model class (labelled or unlabelled)
                    # don't use True or False for the classes as it raises a casting bug in underlying library
                    l_estimator =  self.label_estimators[label_name]
                    l_estimator.partial_fit(doc.get_features(),
                                            [doc_has_label],
                                            numpy.array(['labelled','unlabelled']))
            elif removed_label:
                l_estimator = self.label_estimators[removed_label.name]
                l_estimator.partial_fit(doc.get_features(),
                                        ['unlabelled'],
                                        numpy.array(['labelled','unlabelled']))

    def predict_label_list(self, doc):
        """
        return a prediction of label names
        """
        if doc.nb_pages <= 0:
            return []

        # if there is only one label, or not enough document fitted prediction is not possible
        if len(self.label_estimators) < 2:
            return []

        predicted_label_list = []
        for label_name in self.label_estimators:
            features = doc.get_features()
            # check that the estimator will not throw an error because its not fitted
            if self.label_estimators[label_name].coef_ is None:
                logger.warning("Label estimator '%s' not fitted yet"
                               % label_name)
                continue
            prediction = self.label_estimators[label_name].predict(features)
            if prediction == 'labelled':
                predicted_label_list.append(label_name)
            logger.info("%s %s %s with decision %s "
                         % (doc, prediction, label_name,
                            self.label_estimators[label_name].
                            decision_function(features)))
        return predicted_label_list

    def __inst_doc(self, docid, doc_type_name=None):
        """
        Instantiate a document based on its document id.
        The information are taken from the whoosh index.
        """
        doc = None
        docpath = os.path.join(self.rootdir, docid)
        if not os.path.exists(docpath):
            return None
        if doc_type_name is not None:
            # if we already know the doc type name
            for (is_doc_type, doc_type_name_b, doc_type) in DOC_TYPE_LIST:
                if doc_type_name_b == doc_type_name:
                    doc = doc_type(docpath, docid)
            if not doc:
                logger.warn("Warning: unknown doc type found in the index: %s"
                   % doc_type_name)
        # otherwise we guess the doc type
        if not doc:
            for (is_doc_type, doc_type_name, doc_type) in DOC_TYPE_LIST:
                if is_doc_type(docpath):
                    doc = doc_type(docpath, docid)
        if not doc:
            logger.warn("Warning: unknown doc type for doc '%s'" % docid)

        return doc

    def get_doc_from_docid(self, docid, doc_type_name=None):
        """
        Try to find a document based on its document id. If it hasn't been
        instantiated yet, it will be.
        """
        if docid in self.__docs_by_id:
            return self.__docs_by_id[docid]
        doc = self.__inst_doc(docid, doc_type_name)
        if doc is None:
            return None
        self.__docs_by_id[docid] = doc
        return doc

    def reload_index(self, progress_cb=dummy_progress_cb):
        """
        Read the index, and load the document list from it
        """
        docs_by_id = self.__docs_by_id
        self.__docs_by_id = {}
        for doc in docs_by_id.values():
            doc.drop_cache()
        del docs_by_id

        query = whoosh.query.Every()
        results = self.__searcher.search(query, limit=None)

        nb_results = len(results)
        progress = 0
        labels = set()

        for result in results:
            docid = result['docid']
            doctype = result['doctype']
            doc = self.__inst_doc(docid, doctype)
            if doc is None:
                continue
            progress_cb(progress, nb_results, self.INDEX_STEP_LOADING, doc)
            self.__docs_by_id[docid] = doc
            for label in doc.labels:
                labels.add(label)

            progress += 1
        progress_cb(1, 1, self.INDEX_STEP_LOADING)

        self.label_list = [label for label in labels]
        self.label_list.sort()

    def index_page(self, page):
        """
        Extract all the keywords from the given page

        Arguments:
            page --- from which keywords must be extracted

        Obsolete. To remove. Use get_index_updater() instead
        """
        updater = self.get_index_updater(optimize=False)
        updater.upd_doc(page.doc)
        updater.commit()
        if not page.doc.docid in self.__docs_by_id:
            logger.info("Adding document '%s' to the index" % page.doc.docid)
            assert(page.doc is not None)
            self.__docs_by_id[page.doc.docid] = page.doc

    def __get_all_docs(self):
        """
        Return all the documents. Beware, they are unsorted.
        """
        return self.__docs_by_id.values()

    docs = property(__get_all_docs)

    def get_by_id(self, obj_id):
        """
        Get a document or a page using its ID
        Won't instantiate them if they are not yet available
        """
        if "/" in obj_id:
            (docid, page_nb) = obj_id.split("/")
            page_nb = int(page_nb)
            return self.__docs_by_id[docid].pages[page_nb]
        return self.__docs_by_id[obj_id]

    def find_documents(self, sentence):
        """
        Returns all the documents matching the given keywords

        Arguments:
            sentence --- a sentenced query
        Returns:
            An array of document (doc objects)
        """
        sentence = sentence.strip()

        if sentence == u"":
            return self.docs

        sentence = strip_accents(sentence)

        result_list_list=[]
        for query_parser in self.search_param_list:
            query = query_parser["query_parser"].parse(sentence)
            if "sortedby" in query_parser:
                result_list_list.append(
                    self.__searcher.search(
                        query, limit=None,
                        sortedby = query_parser["sortedby"]))
            else:
                result_list_list.append(
                    self.__searcher.search(
                        query, limit=None))

        # merging results
        results = result_list_list[0]
        for result_intermediate in result_list_list[1:]:
            results.extend(result_intermediate)

        docs = [self.__docs_by_id.get(result['docid']) for result in results]
        try:
            while True:
                docs.remove(None)
        except ValueError:
            pass
        assert (not None in docs)
        return docs

    def find_suggestions(self, sentence):
        """
        Search all possible suggestions. Suggestions returned always have at
        least one document matching.

        Arguments:
            sentence --- keywords (single strings) for which we want
                suggestions
        Return:
            An array of sets of keywords. Each set of keywords (-> one string)
            is a suggestion.
        """
        keywords = sentence.split(" ")
        final_suggestions = []

        corrector = self.__searcher.corrector("content")
        label_corrector = self.__searcher.corrector("label")
        for keyword_idx in range(0, len(keywords)):
            keyword = strip_accents(keywords[keyword_idx])
            if (len(keyword) <= MIN_KEYWORD_LEN):
                continue
            keyword_suggestions = label_corrector.suggest(keyword, limit=2)[:]
            keyword_suggestions += corrector.suggest(keyword, limit=5)[:]
            for keyword_suggestion in keyword_suggestions:
                new_suggestion = keywords[:]
                new_suggestion[keyword_idx] = keyword_suggestion
                new_suggestion = u" ".join(new_suggestion)
                if len(self.find_documents(new_suggestion)) <= 0:
                    continue
                final_suggestions.append(new_suggestion)
        final_suggestions.sort()
        return final_suggestions

    def add_label(self, doc, label):
        """
        Add a label on a document.

        Arguments:
            label --- The new label (see labels.Label)
            doc --- The first document on which this label has been added
        """
        label = copy.copy(label)
        new_label = False
        if not label in self.label_list:
            self.label_list.append(label)
            self.label_list.sort()
            new_label = True
        doc.add_label(label)
        updater = self.get_index_updater(optimize=False)
        updater.upd_doc(doc)
        if new_label:
            # its a brand new label, there is a new estimator.
            # we need to fit this new estimator.
            self.fit_label_estimator(labels=[label])
        updater.commit()

    def remove_label(self, doc, label):
        """
        Remove a label from a doc. Takes care of updating the index
        """
        doc.remove_label(label)
        updater = self.get_index_updater(optimize=False)
        updater.upd_doc(doc)
        self.fit_label_estimator(docs=[doc], removed_label=label)
        updater.commit()

    def update_label(self, old_label, new_label, callback=dummy_progress_cb):
        """
        Replace 'old_label' by 'new_label' on all the documents. Takes care of
        updating the index.
        """
        assert(old_label)
        assert(new_label)
        self.label_list.remove(old_label)
        if old_label.name in self.label_estimators:
            self.label_estimators[new_label.name] = self.label_estimators.pop(old_label.name)
        if new_label not in self.label_list:
            self.label_list.append(new_label)
            self.label_list.sort()
        current = 0
        total = len(self.docs)
        updater = self.get_index_updater(optimize=False)
        for doc in self.docs:
            must_reindex = (old_label in doc.labels)
            callback(current, total, self.LABEL_STEP_UPDATING, doc)
            doc.update_label(old_label, new_label)
            if must_reindex:
                updater.upd_doc(doc)
            current += 1

        updater.commit()

    def destroy_label(self, label, callback=dummy_progress_cb):
        """
        Remove the label 'label' from all the documents. Takes care of updating
        the index.
        """
        assert(label)
        self.label_list.remove(label)
        self.label_estimators.pop(label.name)
        current = 0
        docs = self.docs
        total = len(docs)
        updater = self.get_index_updater(optimize=False)
        for doc in docs:
            must_reindex = (label in doc.labels)
            callback(current, total, self.LABEL_STEP_DESTROYING, doc)
            doc.remove_label(label)
            if must_reindex:
                updater.upd_doc(doc)
            current += 1
        updater.commit()

    def reload_searcher(self):
        """
        When the index has been updated, it's safer to re-instantiate the Whoosh
        Searcher object used to browse it.

        You shouldn't have to call this method yourself.
        """
        searcher = self.__searcher
        self.__searcher = self.index.searcher()
        del(searcher)

    def redo_ocr(self, langs, progress_callback=dummy_progress_cb):
        """
        Rerun the OCR on *all* the documents. Can be a *really* long process,
        which is why progress_callback is a mandatory argument.

        Arguments:
            progress_callback --- See util.dummy_progress_cb for a
                prototype. The only step returned is "INDEX_STEP_READING"
            langs --- Languages to use with the spell checker and the OCR tool
                ( { 'ocr' : 'fra', 'spelling' : 'fr' } )
        """
        logger.info("Redoing OCR of all documents ...")

        dlist = self.docs
        threads = []
        remaining = dlist[:]

        max_threads = multiprocessing.cpu_count()

        while (len(remaining) > 0 or len(threads) > 0):
            for thread in threads:
                if not thread.is_alive():
                    threads.remove(thread)
            while (len(threads) < max_threads and len(remaining) > 0):
                doc = remaining.pop()
                if not doc.can_edit:
                    continue
                thread = threading.Thread(target=doc.redo_ocr,
                                          args=[langs], name=doc.docid)
                thread.start()
                threads.append(thread)
                progress_callback(len(dlist) - len(remaining),
                                  len(dlist), self.INDEX_STEP_READING,
                                  doc)
            time.sleep(self.OCR_THREADS_POLLING_TIME)
        logger.info("OCR of all documents done")

    def destroy_index(self):
        """
        Destroy the index. Don't use this DocSearch object anymore after this
        call. Next instantiation of a DocSearch will rebuild the whole index
        """
        logger.info("Destroying the index ...")
        rm_rf(self.indexdir)
        rm_rf(self.label_estimators_dir)
        logger.info("Done")

    def is_hash_in_index(self, filehash):
        """
        Check if there is a document using this file hash
        """
        results = self.__searcher.search(
               Term('docfilehash', unicode(filehash, "utf-8")))
        return results

"""
Represent a collection of spacy-processed texts as a document-term matrix of shape
(# docs, # unique terms), with a variety of filtering, normalization, and term
weighting schemes for the values.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import collections
import itertools
import operator
from array import array

import numpy as np
import scipy.sparse as sp
from sklearn.preprocessing import normalize as normalize_mat


class Vectorizer(object):
    """
    Transform one or more tokenized documents into a document-term matrix of
    shape (# docs, # unique terms), with tf-, tf-idf, or binary-weighted values.

    Stream a corpus with metadata from disk::

        >>> cw = textacy.datasets.CapitolWords()
        >>> text_stream, metadata_stream = textacy.fileio.split_record_fields(
        ...     cw.records(limit=1000), 'text', itemwise=False)
        >>> corpus = textacy.Corpus('en', texts=text_stream, metadatas=metadata_stream)
        >>> corpus
        Corpus(1000 docs; 537742 tokens)

    Tokenize and vectorize (the first half of) a corpus::

        >>> terms_list = (doc.to_terms_list(ngrams=1, named_entities=True, as_strings=True)
                          for doc in corpus[:500])
        >>> vectorizer = Vectorizer(
        ...     weighting='tfidf', normalize=True, smooth_idf=True,
        ...     min_df=3, max_df=0.95, max_n_terms=100000)
        >>> doc_term_matrix = vectorizer.fit_transform(terms_list)
        >>> doc_term_matrix
        <500x3811 sparse matrix of type '<class 'numpy.float64'>'
               with 54530 stored elements in Compressed Sparse Row format>

    Tokenize and vectorize (the *other* half of) a corpus, using only the terms
    and weights learned in the previous step:

        >>> terms_list = (doc.to_terms_list(ngrams=1, named_entities=True, as_strings=True)
        ...               for doc in corpus[:500])
        >>> doc_term_matrix = vectorizer.transform(terms_list)
        >>> doc_term_matrix
        <500x3811 sparse matrix of type '<class 'numpy.float64'>'
               with 44788 stored elements in Compressed Sparse Row format>

    Args:
        weighting ({'tf', 'tfidf', 'binary'}): Weighting to assign to terms in
            the doc-term matrix. If 'tf', matrix values (i, j) correspond to the
            number of occurrences of term j in doc i; if 'tfidf', term frequencies
            (tf) are multiplied by their corresponding inverse document frequencies
            (idf); if 'binary', all non-zero values are set equal to 1.
        normalize (bool): If True, normalize term frequencies by the
            L2 norms of the vectors.
        binarize (bool): If True, set all term frequencies > 0 equal to 1.
        sublinear_tf (bool): If True, apply sub-linear term-frequency scaling,
            i.e. tf => 1 + log(tf).
        smooth_idf (bool): If True, add 1 to all document frequencies, equivalent
            to adding a single document to the corpus containing every unique term.
        vocabulary (Dict[str, int] or Iterable[str]): Mapping of unique term
            string (str) to unique term id (int) or an iterable of term strings
            (which gets converted into a suitable mapping).
        min_df (float or int): If float, value is the fractional proportion of
            the total number of documents, which must be in [0.0, 1.0]. If int,
            value is the absolute number. Filter terms whose document frequency
            is less than ``min_df``.
        max_df (float or int): If float, value is the fractional proportion of
            the total number of documents, which must be in [0.0, 1.0]. If int,
            value is the absolute number. Filter terms whose document frequency
            is greater than ``max_df``.
        min_ic (float): Filter terms whose information content is less than
            ``min_ic``; value must be in [0.0, 1.0].
        max_n_terms (int): Only include terms whose document frequency is within
            the top ``max_n_terms``.

    Attributes:
        vocabulary (Dict[str, int])
        is_fixed_vocabulary (bool)
        id_to_term (Dict[int, str])
        feature_names (List[str])
    """

    def __init__(self,
                 weighting='tf', normalize=False, sublinear_tf=False, smooth_idf=True,
                 vocabulary=None, min_df=1, max_df=1.0, min_ic=0.0, max_n_terms=None):
        self.weighting = weighting
        self.normalize = normalize
        self.sublinear_tf = sublinear_tf
        self.smooth_idf = smooth_idf
        # sanity check numeric arguments
        if min_df < 0 or max_df < 0:
            raise ValueError('``min_df`` and ``max_df`` must be positive integers or None')
        if min_ic < 0.0 or min_ic > 1.0:
            raise ValueError('``min_ic`` must be a float in the interval [0.0, 1.0]')
        if max_n_terms and max_n_terms < 0:
            raise ValueError('``max_n_terms`` must be a positive integer or None')
        self.min_df = min_df
        self.max_df = max_df
        self.min_ic = min_ic
        self.max_n_terms = max_n_terms
        self.vocabulary, self.is_fixed_vocabulary = self._validate_vocabulary(vocabulary)
        self.id_to_term_ = {}

    def _validate_vocabulary(self, vocabulary):
        """
        Validate an input vocabulary. If it's a mapping, ensure that term ids
        are unique and compact (i.e. without any gaps between 0 and the number
        of terms in ``vocabulary``. If it's a sequence, sort terms then assign
        integer ids in ascending order.
        """
        if vocabulary is not None:
            if not isinstance(vocabulary, collections.Mapping):
                vocab = {}
                for i, term in enumerate(sorted(vocabulary)):
                    if vocab.setdefault(term, i) != i:
                        raise ValueError(
                            'Duplicate term in ``vocabulary``: "{}".'.format(term))
                vocabulary = vocab
            else:
                idxs = set(vocabulary.values())
                if len(idxs) != len(vocabulary):
                    raise ValueError('``vocabulary`` contains repeated indices.')
                for i in range(len(vocabulary)):
                    if i not in idxs:
                        raise ValueError(
                            '``vocabulary`` of {} terms is missing index {}.'.format(
                                len(vocabulary), i))
            if not vocabulary:
                raise ValueError('``vocabulary`` may not be empty.')
            is_fixed_vocabulary = True
        else:
            is_fixed_vocabulary = False
        return vocabulary, is_fixed_vocabulary

    @property
    def id_to_term(self):
        """
        dict: Mapping of unique term id (int) to unique term string (str), i.e.
            the inverse of :attr:`Vectorizer.vocabulary`. This attribute is only
            generated if needed, and it is automatically kept in sync with the
            corresponding vocabulary.
        """
        if len(self.id_to_term_) != self.vocabulary:
            self.id_to_term_ = {
                term_id: term_str for term_str, term_id in self.vocabulary.items()}
        return self.id_to_term_

    @id_to_term.setter
    def id_to_term(self, new_id_to_term):
        self.id_to_term_ = new_id_to_term
        self.vocabulary = {
            term_str: term_id for term_id, term_str in new_id_to_term.items()}

    def fit(self, terms_list):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

        Returns:
            :class:`Vectorizer`: The instance that has just been fit.
        """
        _ = self.fit_transform(terms_list)
        return self

    def fit_transform(self, terms_list):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``, then transform the ``terms_list`` into a document-term
        matrix with values weighted according to the parameters specified in
        ``Vectorizer`` initialization.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

        Returns:
            :class:`scipy.sparse.csr_matrix`: The transformed document-term matrix.
            Rows correspond to documents and columns correspond to terms.
        """
        # count terms and build up a vocabulary
        doc_term_matrix, self.vocabulary = self._count_terms(
            terms_list, self.is_fixed_vocabulary)

        # filter terms by doc freq or info content, as specified in init
        doc_term_matrix, self.vocabulary = self._filter_terms(
            doc_term_matrix, self.vocabulary)

        # re-weight values in doc-term matrix, as specified in init
        doc_term_matrix = self._reweight_values(doc_term_matrix)

        return doc_term_matrix

    def transform(self, terms_list):
        """
        Transform the ``terms_list`` into a document-term matrix with values
        weighted according to the parameters specified in ``Vectorizer``
        initialization.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

        Returns:
            :class:`scipy.sparse.csr_matrix`: The transformed document-term matrix.
            Rows correspond to documents and columns correspond to terms.

        Note:
            This requires an existing vocabulary, either built when calling
            :meth:`Vectorizer.fit()` or provided in ``Vectorizer`` initialization.
        """
        self._check_vocabulary()
        doc_term_matrix, _ = self._count_terms(
            terms_list, True)
        return self._reweight_values(doc_term_matrix)

    def _count_terms(self, terms_list, fixed_vocab):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``.

        Args:
            terms_lists (Iterable[Iterable[str]]): A sequence of documents, each
                as a sequence of (str) terms.
            fixed_vocab (bool): If False, a new vocabulary is built from terms
                in ``terms_list``; if True, only terms already found in the
                :attr:`Vectorizer.vocabulary` are counted.

        Returns:
            :class:`scipy.sparse.csr_matrix`

            dict
        """
        if fixed_vocab is False:
            # add a new value when a new vocabulary item is seen
            vocabulary = collections.defaultdict()
            vocabulary.default_factory = vocabulary.__len__
        else:
            vocabulary = self.vocabulary

        data = array(str('i'))
        indices = array(str('i'))
        indptr = array(str('i'), [0])
        for terms in terms_list:
            term_counter = collections.defaultdict(int)
            for term in terms:
                try:
                    term_idx = vocabulary[term]
                    term_counter[term_idx] += 1
                except KeyError:
                    # ignore out-of-vocabulary terms when is_fixed_vocabulary=True
                    continue

            data.extend(term_counter.values())
            indices.extend(term_counter.keys())
            indptr.append(len(indices))

        if fixed_vocab is False:
            # we no longer want defaultdict behaviour
            vocabulary = dict(vocabulary)

        data = np.frombuffer(data, dtype=np.intc)
        indices = np.asarray(indices, dtype=np.intc)
        indptr = np.frombuffer(indptr, dtype=np.intc)

        doc_term_matrix = sp.csr_matrix(
            (data, indices, indptr),
            shape=(len(indptr) - 1, len(vocabulary)),
            dtype=np.int32)
        doc_term_matrix.sort_indices()

        return doc_term_matrix, vocabulary

    def _filter_terms(self, doc_term_matrix, vocabulary):
        """
        Filter terms in ``vocabulary`` by their document frequency or information
        content, as specified in ``Vectorizer`` initialization.

        Args:
            doc_term_matrix (:class:`sp.sparse.csr_matrix`): Sparse matrix of
                shape (# docs, # unique terms), where value (i, j) is the weight
                of term j in doc i.
            vocabulary (Dict[str, int]): Mapping of term strings to their unique
                integer ids, like ``{"hello": 0, "world": 1}``.

        Returns:
            :class:`scipy.sparse.csr_matrix`

            Dict[str, int]
        """
        if self.is_fixed_vocabulary:
            return doc_term_matrix, vocabulary
        else:
            if self.max_df != 1.0 or self.min_df != 1 or self.max_n_terms is not None:
                doc_term_matrix, vocabulary = filter_terms_by_df(
                    doc_term_matrix, vocabulary,
                    max_df=self.max_df, min_df=self.min_df, max_n_terms=self.max_n_terms)
            if self.min_ic != 0.0:
                doc_term_matrix, vocabulary = filter_terms_by_ic(
                    doc_term_matrix, vocabulary,
                    min_ic=self.min_ic, max_n_terms=self.max_n_terms)
            return doc_term_matrix, vocabulary

    def _reweight_values(self, doc_term_matrix):
        """
        Re-weight values in a doc-term matrix according to parameters specified
        in ``Vectorizer`` initialization: binary or tf-idf weighting, sublinear
        term-frequency, document-normalized weights.

        Args:
            doc_term_matrix (:class:`sp.sparse.csr_matrix`): Sparse matrix of
                shape (# docs, # unique terms), where value (i, j) is the weight
                of term j in doc i.

        Returns:
            :class:`scipy.sparse.csr_matrix`: Re-weighted doc-term matrix.
        """
        if self.weighting == 'binary':
            doc_term_matrix.data.fill(1)
        else:
            if self.sublinear_tf is True:
                doc_term_matrix = doc_term_matrix.astype(np.float64)
                _ = np.log(doc_term_matrix.data, doc_term_matrix.data)
                doc_term_matrix.data += 1
            if self.weighting == 'tfidf':
                doc_term_matrix = apply_idf_weighting(
                    doc_term_matrix,
                    smooth_idf=self.smooth_idf)

        if self.normalize is True:
            doc_term_matrix = normalize_mat(
                doc_term_matrix,
                norm='l2', axis=1, copy=False)

        return doc_term_matrix

    @property
    def feature_names(self):
        """Array mapping from feature integer indices to feature name."""
        self._check_vocabulary()
        return [term_str for term_str, _
                in sorted(self.vocabulary.items(), key=operator.itemgetter(1))]

    def _check_vocabulary(self):
        if not isinstance(self.vocabulary, collections.Mapping):
            raise ValueError(
                'vocabulary hasn\'t been built; call ``Vectorizer.fit()``')
        if len(self.vocabulary) == 0:
            raise ValueError('vocabulary is empty')


class GroupVectorizer(object):
    """
    Transform one or more tokenized documents into a group-term matrix of
    shape (# groups, # unique terms), with tf-, tf-idf, or binary-weighted values.

    Stream a corpus with metadata from disk::

        >>> cw = textacy.datasets.CapitolWords()
        >>> text_stream, metadata_stream = textacy.fileio.split_record_fields(
        ...     cw.records(limit=1000), 'text', itemwise=False)
        >>> corpus = textacy.Corpus('en', texts=text_stream, metadatas=metadata_stream)
        >>> corpus
        Corpus(1000 docs; 538172 tokens)

    Tokenize and vectorize the first 600 documents of this corpus::

        >>> terms_lists, groups = textacy.filio.utils.unzip(
        ...     (doc.to_terms_list(ngrams=False, named_entities=True, as_strings=True),
        ...      doc.metadata['speaker_name'])
        ...     for doc in corpus[:600])
        ... )
        >>> vectorizer = GroupVectorizer(
        ...     weighting='tfidf', normalize=True, smooth_idf=True,
        ...     min_df=3, max_df=0.95, max_n_terms=100000)
        >>> grp_term_matrix = vectorizer.fit_transform(terms_list, groups)
        >>> grp_term_matrix
        <5x212 sparse matrix of type '<class 'numpy.float64'>'
        	with 704 stored elements in Compressed Sparse Row format>

    Tokenize and vectorize the remaining 400 documents of the corpus, using only
    the groups, terms, and weights learned in the previous step:

        >>> terms_lists, groups = textacy.filio.utils.unzip(
        ...     (doc.to_terms_list(ngrams=False, named_entities=True, as_strings=True),
        ...      doc.metadata['speaker_name'])
        ...     for doc in corpus[600:])
        ... )
        >>> grp_term_matrix = vectorizer.transform(terms_list, groups)
        >>> grp_term_matrix
        <5x212 sparse matrix of type '<class 'numpy.float64'>'
        	with 392 stored elements in Compressed Sparse Row format>

    Args:
        weighting ({'tf', 'tfidf', 'binary'}): Weighting to assign to terms in
            the doc-term matrix. If 'tf', matrix values (i, j) correspond to the
            number of occurrences of term j in doc i; if 'tfidf', term frequencies
            (tf) are multiplied by their corresponding inverse document frequencies
            (idf); if 'binary', all non-zero values are set equal to 1.
        normalize (bool): If True, normalize term frequencies by the
            L2 norms of the vectors.
        binarize (bool): If True, set all term frequencies > 0 equal to 1.
        sublinear_tf (bool): If True, apply sub-linear term-frequency scaling,
            i.e. tf => 1 + log(tf).
        smooth_idf (bool): If True, add 1 to all document frequencies, equivalent
            to adding a single document to the corpus containing every unique term.
        vocabulary (Dict[str, int] or Iterable[str]): Mapping of unique term
            string (str) to unique term id (int) or an iterable of term strings
            (which gets converted into a suitable mapping).
        min_df (float or int): If float, value is the fractional proportion of
            the total number of documents, which must be in [0.0, 1.0]. If int,
            value is the absolute number. Filter terms whose document frequency
            is less than ``min_df``.
        max_df (float or int): If float, value is the fractional proportion of
            the total number of documents, which must be in [0.0, 1.0]. If int,
            value is the absolute number. Filter terms whose document frequency
            is greater than ``max_df``.
        min_ic (float): Filter terms whose information content is less than
            ``min_ic``; value must be in [0.0, 1.0].
        max_n_terms (int): Only include terms whose document frequency is within
            the top ``max_n_terms``.

    Attributes:
        vocabulary (Dict[str, int])
        is_fixed_vocabulary (bool)
        id_to_term (Dict[int, str])
        feature_names (List[str])
    """

    def __init__(self,
                 weighting='tf', normalize=False, sublinear_tf=False, smooth_idf=True,
                 min_df=1, max_df=1.0, min_ic=0.0, max_n_terms=None,
                 vocabulary_terms=None, vocabulary_grps=None):
        self.weighting = weighting
        self.normalize = normalize
        self.sublinear_tf = sublinear_tf
        self.smooth_idf = smooth_idf
        # sanity check numeric arguments
        if min_df < 0 or max_df < 0:
            raise ValueError('``min_df`` and ``max_df`` must be positive integers or None')
        if min_ic < 0.0 or min_ic > 1.0:
            raise ValueError('``min_ic`` must be a float in the interval [0.0, 1.0]')
        if max_n_terms and max_n_terms < 0:
            raise ValueError('``max_n_terms`` must be a positive integer or None')
        self.min_df = min_df
        self.max_df = max_df
        self.min_ic = min_ic
        self.max_n_terms = max_n_terms
        self.vocabulary_terms, self.is_fixed_vocabulary_terms = self._validate_vocabulary(vocabulary_terms)
        self.vocabulary_grps, self.is_fixed_vocabulary_grps = self._validate_vocabulary(vocabulary_grps)
        self.id_to_term_ = {}
        self.id_to_group_ = {}

    def _validate_vocabulary(self, vocabulary):
        """
        Validate an input vocabulary. If it's a mapping, ensure that term ids
        are unique and compact (i.e. without any gaps between 0 and the number
        of terms in ``vocabulary``. If it's a sequence, sort terms then assign
        integer ids in ascending order.
        """
        if vocabulary is not None:
            if not isinstance(vocabulary, collections.Mapping):
                vocab = {}
                for i, term in enumerate(sorted(vocabulary)):
                    if vocab.setdefault(term, i) != i:
                        raise ValueError(
                            'Duplicate term in ``vocabulary``: "{}".'.format(term))
                vocabulary = vocab
            else:
                idxs = set(vocabulary.values())
                if len(idxs) != len(vocabulary):
                    raise ValueError('``vocabulary`` contains repeated indices.')
                for i in range(len(vocabulary)):
                    if i not in idxs:
                        raise ValueError(
                            '``vocabulary`` of {} terms is missing index {}.'.format(
                                len(vocabulary), i))
            if not vocabulary:
                raise ValueError('``vocabulary`` may not be empty.')
            is_fixed_vocabulary = True
        else:
            is_fixed_vocabulary = False
        return vocabulary, is_fixed_vocabulary

    @property
    def id_to_term(self):
        """
        dict: Mapping of unique term id (int) to unique term string (str), i.e.
            the inverse of :attr:`GroupVectorizer.vocabulary_terms`. This attribute is only
            generated if needed, and it is automatically kept in sync with the
            corresponding vocabulary.
        """
        if len(self.id_to_term_) != self.vocabulary_terms:
            self.id_to_term_ = {
                term_id: term_str for term_str, term_id in self.vocabulary_terms.items()}
        return self.id_to_term_

    @id_to_term.setter
    def id_to_term(self, new_id_to_term):
        self.id_to_term_ = new_id_to_term
        self.vocabulary_terms = {
            term_str: term_id for term_id, term_str in new_id_to_term.items()}

    @property
    def id_to_grp(self):
        """
        dict: Mapping of unique group id (int) to unique group string (str), i.e.
            the inverse of :attr:`GroupVectorizer.vocabulary_grps`. This attribute is only
            generated if needed, and it is automatically kept in sync with the
            corresponding vocabulary.
        """
        if len(self.id_to_grp_) != self.vocabulary_grps:
            self.id_to_grp_ = {
                grp_id: grp_str for grp_str, grp_id in self.vocabulary_grps.items()}
        return self.id_to_grp_

    @id_to_grp.setter
    def id_to_grp(self, new_id_to_grp):
        self.id_to_grp_ = new_id_to_grp
        self.vocabulary_grps = {
            grp_str: grp_id for grp_id, grp_str in new_id_to_grp.items()}

    def fit(self, terms_list, grps):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

            grps (Iterable[str])

        Returns:
            :class:`GroupVectorizer`: The instance that has just been fit.
        """
        _ = self.fit_transform(terms_list, grps)
        return self

    def fit_transform(self, terms_list, grps):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``, then transform the ``terms_list`` into a document-term
        matrix with values weighted according to the parameters specified in
        ``Vectorizer`` initialization.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

            grps (Iterable[str])

        Returns:
            :class:`scipy.sparse.csr_matrix`: The transformed document-term matrix.
            Rows correspond to documents and columns correspond to terms.
        """
        # count terms and build up a vocabulary
        grp_term_matrix, self.vocabulary_terms, self.vocabulary_grps = self._count_terms(
            terms_list, grps,
            self.is_fixed_vocabulary_terms, self.is_fixed_vocabulary_grps)

        # filter terms by group freq or info content, as specified in init
        grp_term_matrix, self.vocabulary_terms = self._filter_terms(
            grp_term_matrix, self.vocabulary_terms)

        # re-weight values in group-term matrix, as specified in init
        grp_term_matrix = self._reweight_values(grp_term_matrix)

        return grp_term_matrix

    def transform(self, terms_list, grps):
        """
        Transform the ``terms_list`` into a document-term matrix with values
        weighted according to the parameters specified in ``Vectorizer``
        initialization.

        Args:
            terms_list (Iterable[Iterable[str]]): A sequence of tokenized documents,
                where each document is a sequence of (str) terms. For example::

                    >>> ([tok.lemma_ for tok in spacy_doc]
                    ...  for spacy_doc in spacy_docs)
                    >>> ((ne.text for ne in extract.named_entities(doc))
                    ...  for doc in corpus)
                    >>> (tuple(ng.text for ng in
                    ...        itertools.chain.from_iterable(extract.ngrams(doc, i)
                    ...                                      for i in range(1, 3)))
                    ...  for doc in docs)

            grps (Iterable[str])

        Returns:
            :class:`scipy.sparse.csr_matrix`: The transformed document-term matrix.
            Rows correspond to documents and columns correspond to terms.

        Note:
            This requires an existing vocabulary, either built when calling
            :meth:`Vectorizer.fit()` or provided in ``Vectorizer`` initialization.
        """
        self._check_vocabulary()
        grp_term_matrix, _, _ = self._count_terms(
            terms_list, grps, True, True)
        return self._reweight_values(grp_term_matrix)

    def _count_terms(self, terms_list, grps, fixed_vocab_terms, fixed_vocab_grps):
        """
        Count terms and build up a vocabulary based on the terms found in the
        ``terms_list``.

        Args:
            terms_lists (Iterable[Iterable[str]]): A sequence of documents, each
                as a sequence of (str) terms.
            fixed_vocab (bool): If False, a new vocabulary is built from terms
                in ``terms_list``; if True, only terms already found in the
                :attr:`Vectorizer.vocabulary` are counted.

        Returns:
            :class:`scipy.sparse.csr_matrix`

            dict
        """
        if fixed_vocab_terms is False:
            # add a new value when a new vocabulary item is seen
            vocabulary_terms = collections.defaultdict()
            vocabulary_terms.default_factory = vocabulary_terms.__len__
        else:
            vocabulary_terms = self.vocabulary_terms

        if fixed_vocab_grps is False:
            # add a new value when a new vocabulary item is seen
            vocabulary_grps = collections.defaultdict()
            vocabulary_grps.default_factory = vocabulary_grps.__len__
        else:
            vocabulary_grps = self.vocabulary_grps

        data = array(str('i'))
        rows = array(str('i'))
        cols = array(str('i'))

        for grp, terms in zip(grps, terms_list):

            try:
                grp_idx = vocabulary_grps[grp]
            except KeyError:
                # ignore out-of-vocabulary groups when is_fixed_vocabulary_grps=True
                continue

            term_counter = collections.defaultdict(int)
            for term in terms:
                try:
                    term_idx = vocabulary_terms[term]
                    term_counter[term_idx] += 1
                except KeyError:
                    # ignore out-of-vocabulary terms when is_fixed_vocabulary=True
                    continue

            data.extend(term_counter.values())
            rows.extend(grp_idx for _ in range(len(term_counter)))
            cols.extend(term_counter.keys())

        # do we still want defaultdict behaviour?
        if fixed_vocab_terms is False:
            vocabulary_terms = dict(vocabulary_terms)
        if fixed_vocab_grps is False:
            vocabulary_grps = dict(vocabulary_grps)

        data = np.asarray(data, dtype=np.intc)
        rows = np.asarray(rows, dtype=np.intc)
        cols = np.asarray(cols, dtype=np.intc)

        grp_term_matrix = sp.csr_matrix(
            (data, (rows, cols)),
            shape=(len(vocabulary_grps), len(vocabulary_terms)),
            dtype=np.int32)
        grp_term_matrix.sort_indices()

        return grp_term_matrix, vocabulary_terms, vocabulary_grps

    def _filter_terms(self, grp_term_matrix, vocabulary):
        """
        Filter terms in ``vocabulary`` by their document frequency or information
        content, as specified in ``Vectorizer`` initialization.

        Args:
            grp_term_matrix (:class:`sp.sparse.csr_matrix`): Sparse matrix of
                shape (# groups, # unique terms), where value (i, j) is the weight
                of term j in group i.
            vocabulary (Dict[str, int]): Mapping of term strings to their unique
                integer ids, like ``{"hello": 0, "world": 1}``.

        Returns:
            :class:`scipy.sparse.csr_matrix`

            Dict[str, int]
        """
        if self.is_fixed_vocabulary_terms:
            return grp_term_matrix, vocabulary
        else:
            if self.max_df != 1.0 or self.min_df != 1 or self.max_n_terms is not None:
                grp_term_matrix, vocabulary = filter_terms_by_df(
                    grp_term_matrix, vocabulary,
                    max_df=self.max_df, min_df=self.min_df, max_n_terms=self.max_n_terms)
            if self.min_ic != 0.0:
                grp_term_matrix, vocabulary = filter_terms_by_ic(
                    grp_term_matrix, vocabulary,
                    min_ic=self.min_ic, max_n_terms=self.max_n_terms)
            return grp_term_matrix, vocabulary

    def _reweight_values(self, grp_term_matrix):
        """
        Re-weight values in a group-term matrix according to parameters specified
        in ``Vectorizer`` initialization: binary or tf-idf weighting, sublinear
        term-frequency, document-normalized weights.

        Args:
            grp_term_matrix (:class:`sp.sparse.csr_matrix`): Sparse matrix of
                shape (# groups, # unique terms), where value (i, j) is the weight
                of term j in doc i.

        Returns:
            :class:`scipy.sparse.csr_matrix`: Re-weighted group-term matrix.
        """
        if self.weighting == 'binary':
            grp_term_matrix.data.fill(1)
        else:
            if self.sublinear_tf is True:
                grp_term_matrix = grp_term_matrix.astype(np.float64)
                _ = np.log(grp_term_matrix.data, grp_term_matrix.data)
                grp_term_matrix.data += 1
            if self.weighting == 'tfidf':
                grp_term_matrix = apply_idf_weighting(
                    grp_term_matrix,
                    smooth_idf=self.smooth_idf)

        if self.normalize is True:
            grp_term_matrix = normalize_mat(
                grp_term_matrix,
                norm='l2', axis=1, copy=False)

        return grp_term_matrix

    @property
    def feature_names(self):
        """Array mapping from feature integer indices to feature name."""
        self._check_vocabulary()
        return [term_str for term_str, _
                in sorted(self.vocabulary_terms.items(), key=operator.itemgetter(1))]

    @property
    def group_names(self):
        """Array mapping from group integer indices to group name."""
        self._check_vocabulary()
        return [grp_str for grp_str, _
                in sorted(self.vocabulary_grps.items(), key=operator.itemgetter(1))]

    def _check_vocabulary(self):
        if not isinstance(self.vocabulary_terms, collections.Mapping):
            raise ValueError(
                'vocabulary hasn\'t been built; call ``GroupVectorizer.fit()``')
        if len(self.vocabulary_terms) == 0:
            raise ValueError('vocabulary is empty')


def apply_idf_weighting(doc_term_matrix, smooth_idf=True):
    """
    Apply inverse document frequency (idf) weighting to a term-frequency (tf)
    weighted document-term matrix, optionally smoothing idf values.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix`):
            M X N matrix, where M is the # of docs and N is the # of unique terms
        smooth_idf (bool): if True, add 1 to all document frequencies, equivalent
            to adding a single document to the corpus containing every unique term

    Returns:
        :class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix>`: sparse matrix
        of shape (# docs, # unique terms), where value (i, j) is the tfidf
        weight of term j in doc i
    """
    dfs = get_doc_freqs(doc_term_matrix, normalized=False)
    n_docs, _ = doc_term_matrix.shape
    if smooth_idf is True:
        n_docs += 1
        dfs += 1
    idfs = np.log(n_docs / dfs) + 1.0
    return doc_term_matrix.dot(sp.diags(idfs, 0))


def get_term_freqs(doc_term_matrix, normalized=True):
    """
    Compute absolute or relative term frequencies for all terms in a
    document-term matrix.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix`):
            M X N matrix, where M is the # of docs and N is the # of unique terms

            Note: Weighting on the terms DOES matter! Only absolute term counts
            (rather than normalized term frequencies) should be used here
        normalized (bool): if True, return normalized term frequencies, i.e.
            term counts divided by the total number of terms; if False, return
            absolute term counts

    Returns:
        :class:`numpy.ndarray <numpy.ndarray>`: array of absolute or relative term
        frequencies, with length equal to the # of unique terms, i.e. # of
        columns in ``doc_term_matrix``

    Raises:
        ValueError: if ``doc_term_matrix`` doesn't have any non-zero entries
    """
    if doc_term_matrix.nnz == 0:
        raise ValueError('term-document matrix must have at least 1 non-zero entry')
    _, n_terms = doc_term_matrix.shape
    tfs = np.asarray(doc_term_matrix.sum(axis=0)).ravel()
    if normalized is True:
        return tfs / n_terms
    else:
        return tfs


def get_doc_freqs(doc_term_matrix, normalized=True):
    """
    Compute absolute or relative document frequencies for all terms in a
    term-document matrix.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix`):
            M X N matrix, where M is the # of docs and N is the # of unique terms

            Note: Weighting on the terms doesn't matter! Could be 'tf' or 'tfidf'
            or 'binary' weighting, a term's doc freq will be the same
        normalized (bool): if True, return normalized doc frequencies, i.e.
            doc counts divided by the total number of docs; if False, return
            absolute doc counts

    Returns:
        :class:`numpy.ndarray`: array of absolute or relative document
        frequencies, with length equal to the # of unique terms, i.e. # of
        columns in ``doc_term_matrix``

    Raises:
        ValueError: if ``doc_term_matrix`` doesn't have any non-zero entries
    """
    if doc_term_matrix.nnz == 0:
        raise ValueError('term-document matrix must have at least 1 non-zero entry')
    n_docs, n_terms = doc_term_matrix.shape
    dfs = np.bincount(doc_term_matrix.indices, minlength=n_terms)
    if normalized is True:
        return dfs / n_docs
    else:
        return dfs


def get_information_content(doc_term_matrix):
    """
    Compute information content for all terms in a term-document matrix. IC is a
    float in [0.0, 1.0], defined as ``-df * log2(df) - (1 - df) * log2(1 - df)``,
    where df is a term's normalized document frequency.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix`):
            M X N matrix, where M is the # of docs and N is the # of unique terms

            Note: Weighting on the terms doesn't matter! Could be 'tf' or 'tfidf'
            or 'binary' weighting, a term's information content will be the same

    Returns:
        :class:`numpy.ndarray`: array of term information content values,
        with length equal to the # of unique terms, i.e. # of
        columns in ``doc_term_matrix``

    Raises:
        ValueError: if ``doc_term_matrix`` doesn't have any non-zero entries
    """
    dfs = get_doc_freqs(doc_term_matrix, normalized=True)
    ics = -dfs * np.log2(dfs) - (1 - dfs) * np.log2(1 - dfs)
    ics[np.isnan(ics)] = 0.0  # NaN values not permitted!
    return ics


def filter_terms_by_df(doc_term_matrix, term_to_id,
                       max_df=1.0, min_df=1, max_n_terms=None):
    """
    Filter out terms that are too common and/or too rare (by document frequency),
    and compactify the top ``max_n_terms`` in the ``id_to_term`` mapping accordingly.
    Borrows heavily from the ``sklearn.feature_extraction.text`` module.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix`): M X N matrix, where
            M is the # of docs and N is the # of unique terms.
        term_to_id (Dict[str, int]): Mapping of term string to unique term id,
            e.g. :attr:`Vectorizer.vocabulary`.
        min_df (float or int): if float, value is the fractional proportion of
            the total number of documents and must be in [0.0, 1.0]; if int,
            value is the absolute number; filter terms whose document frequency
            is less than ``min_df``
        max_df (float or int): if float, value is the fractional proportion of
            the total number of documents and must be in [0.0, 1.0]; if int,
            value is the absolute number; filter terms whose document frequency
            is greater than ``max_df``
        max_n_terms (int): only include terms whose *term* frequency is within
            the top `max_n_terms`

    Returns:
        :class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix>`: sparse matrix
        of shape (# docs, # unique *filtered* terms), where value (i, j) is the
        weight of term j in doc i

        dict: id to term mapping, where keys are unique *filtered* integers as
        term ids and values are corresponding strings

    Raises:
        ValueError: if ``max_df`` or ``min_df`` or ``max_n_terms`` < 0
    """
    if max_df == 1.0 and min_df == 1 and max_n_terms is None:
        return doc_term_matrix, term_to_id
    if max_df < 0 or min_df < 0 or (max_n_terms is not None and max_n_terms < 0):
        raise ValueError('max_df, min_df, and max_n_terms may not be negative')

    n_docs, n_terms = doc_term_matrix.shape
    max_doc_count = max_df if isinstance(max_df, int) else int(max_df * n_docs)
    min_doc_count = min_df if isinstance(min_df, int) else int(min_df * n_docs)
    if max_doc_count < min_doc_count:
        raise ValueError('max_df corresponds to fewer documents than min_df')

    # calculate a mask based on document frequencies
    dfs = get_doc_freqs(doc_term_matrix, normalized=False)
    mask = np.ones(n_terms, dtype=bool)
    if max_doc_count < n_docs:
        mask &= dfs <= max_doc_count
    if min_doc_count > 1:
        mask &= dfs >= min_doc_count
    if max_n_terms is not None and mask.sum() > max_n_terms:
        tfs = get_term_freqs(doc_term_matrix, normalized=False)
        top_mask_inds = (tfs[mask]).argsort()[::-1][:max_n_terms]
        new_mask = np.zeros(n_terms, dtype=bool)
        new_mask[np.where(mask)[0][top_mask_inds]] = True
        mask = new_mask

    # map old term indices to new ones
    new_indices = np.cumsum(mask) - 1
    term_to_id = {term: new_indices[old_index]
                  for term, old_index in term_to_id.items()
                  if mask[old_index]}

    kept_indices = np.where(mask)[0]
    if len(kept_indices) == 0:
        msg = 'After filtering, no terms remain; try a lower `min_df` or higher `max_df`'
        raise ValueError(msg)

    return (doc_term_matrix[:, kept_indices], term_to_id)


def filter_terms_by_ic(doc_term_matrix, term_to_id,
                       min_ic=0.0, max_n_terms=None):
    """
    Filter out terms that are too common and/or too rare (by information content),
    and compactify the top ``max_n_terms`` in the ``id_to_term`` mapping accordingly.
    Borrows heavily from the ``sklearn.feature_extraction.text`` module.

    Args:
        doc_term_matrix (:class:`scipy.sparse.csr_matrix`): M X N matrix, where
            M is the # of docs and N is the # of unique terms.
        term_to_id (Dict[str, int]): Mapping of term string to unique term id,
            e.g. :attr:`Vectorizer.vocabulary`.
        min_ic (float): filter terms whose information content is less than this
            value; must be in [0.0, 1.0]
        max_n_terms (int): only include terms whose information content is within
            the top ``max_n_terms``

    Returns:
        :class:`scipy.sparse.csr_matrix <scipy.sparse.csr_matrix>`: sparse matrix
        of shape (# docs, # unique *filtered* terms), where value (i, j) is the
        weight of term j in doc i

        dict: id to term mapping, where keys are unique *filtered* integers as
        term ids and values are corresponding strings

    Raises:
        ValueError: if ``min_ic`` not in [0.0, 1.0] or ``max_n_terms`` < 0
    """
    if min_ic == 0.0 and max_n_terms is None:
        return doc_term_matrix, term_to_id
    if min_ic < 0.0 or min_ic > 1.0:
        raise ValueError('min_ic must be a float in [0.0, 1.0]')
    if max_n_terms is not None and max_n_terms < 0:
        raise ValueError('max_n_terms may not be negative')

    _, n_terms = doc_term_matrix.shape

    # calculate a mask based on document frequencies
    ics = get_information_content(doc_term_matrix)
    mask = np.ones(n_terms, dtype=bool)
    if min_ic > 0.0:
        mask &= ics >= min_ic
    if max_n_terms is not None and mask.sum() > max_n_terms:
        top_mask_inds = (ics[mask]).argsort()[::-1][:max_n_terms]
        new_mask = np.zeros(n_terms, dtype=bool)
        new_mask[np.where(mask)[0][top_mask_inds]] = True
        mask = new_mask

    # map old term indices to new ones
    new_indices = np.cumsum(mask) - 1
    term_to_id = {term: new_indices[old_index]
                  for term, old_index in term_to_id.items()
                  if mask[old_index]}

    kept_indices = np.where(mask)[0]
    if len(kept_indices) == 0:
        raise ValueError('After filtering, no terms remain; try a lower `min_ic`')

    return (doc_term_matrix[:, kept_indices], term_to_id)

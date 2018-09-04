import logging

from fonduer.candidates.models import Candidate
from fonduer.supervision.models import GoldLabelKey, Label, LabelKey
from fonduer.utils.udf import UDF, UDFRunner
from fonduer.utils.utils_udf import (
    ALL_SPLITS,
    add_keys,
    batch_upsert_records,
    get_cands_list_from_split,
    get_docs_from_split,
    get_mapping,
    get_sparse_matrix,
)

logger = logging.getLogger(__name__)


def load_gold_labels(session, cand_lists, annotator_name="gold"):
    """Load the sparse matrix for the specified annotator."""
    return get_sparse_matrix(session, GoldLabelKey, cand_lists, key=annotator_name)


class Labeler(UDFRunner):
    """An operator to add Label Annotations to Candidates."""

    def __init__(self, session, candidate_classes):
        """Initialize the Labeler."""
        super(Labeler, self).__init__(
            session, LabelerUDF, candidate_classes=candidate_classes
        )
        self.candidate_classes = candidate_classes
        self.lfs = []

    def update(self, docs=None, split=0, lfs=None, **kwargs):
        """Update the labels of the specified candidates based on the provided LFs.

        :param docs: If provided, apply the updated LFs to all the candidates
            in these documents.
        :param split: If docs is None, apply the updated LFs to the candidates
            in this particular split.
        :param lfs: A list of lists of labeling functions to update. Each list
            should correspond with the candidate_classes used to initialize the
            Labeler.
        """
        if lfs is None:
            raise ValueError("Please provide a list of lists of labeling functions.")

        if len(lfs) != len(self.candidate_classes):
            raise ValueError("Please provide LFs for each candidate class.")

        for i in range(len(self.lfs)):
            # Filter out the new/updated LFs
            self.lfs[i] = [
                lf
                for lf in self.lfs[i]
                if lf.__name__ not in [_.__name__ for _ in lfs[i]]
            ]
            # Then add them
            self.lfs[i].extend(lfs[i])

        self.apply(
            docs=docs, split=split, lfs=self.lfs, train=True, clear=False, **kwargs
        )

    def apply(self, docs=None, split=0, train=False, lfs=None, clear=True, **kwargs):
        """Apply the labels of the specified candidates based on the provided LFs.

        :param docs: If provided, apply the LFs to all the candidates in these
            documents.
        :param split: If docs is None, apply the LFs to the candidates in this
            particular split.
        :param train: Whether or not to update the global key set of labels and
            the labels of candidates.
        :param lfs: A list of lists of labeling functions to apply. Each list
            should correspond with the candidate_classes used to initialize the
            Labeler.
        :param clear: Whether or not to clear the labels table before applying
            these LFs.
        """
        if lfs is None:
            raise ValueError("Please provide a list of labeling functions.")

        if len(lfs) != len(self.candidate_classes):
            raise ValueError("Please provide LFs for each candidate class.")

        self.lfs = lfs
        if docs:
            # Call apply on the specified docs for all splits
            split = ALL_SPLITS
            super(Labeler, self).apply(
                docs, split=split, train=train, lfs=self.lfs, clear=clear, **kwargs
            )
            # Needed to sync the bulk operations
            self.session.commit()
        else:
            # Only grab the docs containing candidates from the given split.
            split_docs = get_docs_from_split(
                self.session, self.candidate_classes, split
            )
            super(Labeler, self).apply(
                split_docs,
                split=split,
                train=train,
                lfs=self.lfs,
                clear=clear,
                **kwargs
            )
            # Needed to sync the bulk operations
            self.session.commit()

    def get_lfs(self):
        """Return a list of lists of labeling functions for this Labeler."""
        return self.lfs

    def drop_keys(self, keys):
        """Drop the specified keys from LabelKeys."""
        # Make sure keys is iterable
        keys = keys if isinstance(keys, (list, tuple)) else [keys]

        # Remove the specified keys
        for key in keys:
            try:  # Assume key is an LF
                self.session.query(LabelKey).filter(
                    LabelKey.name == key.__name__
                ).delete()
            except AttributeError:
                self.session.query(LabelKey).filter(LabelKey.name == key).delete()

    def clear(self, train=False, split=0, **kwargs):
        """Delete Labels of each class from the database."""
        # Clear Labels for the candidates in the split passed in.
        logger.info("Clearing Labels (split {})".format(split))

        sub_query = (
            self.session.query(Candidate.id).filter(Candidate.split == split).subquery()
        )
        query = self.session.query(Label).filter(Label.candidate_id.in_(sub_query))
        query.delete(synchronize_session="fetch")

        # Delete all old annotation keys
        if train:
            logger.debug("Clearing all LabelKey...")
            query = self.session.query(LabelKey)
            query.delete(synchronize_session="fetch")

    def clear_all(self, **kwargs):
        """Delete all Labels."""
        logger.info("Clearing ALL Labels and LabelKeys.")
        self.session.query(Label).delete()
        self.session.query(LabelKey).delete()

    def get_gold_labels(self, cand_lists, annotator=None):
        """Load sparse matrix of GoldLabels for each candidate_class."""
        return get_sparse_matrix(self.session, GoldLabelKey, cand_lists, key=annotator)

    def get_label_matrices(self, cand_lists):
        """Load sparse matrix of Labels for each candidate_class."""
        return get_sparse_matrix(self.session, LabelKey, cand_lists)


class LabelerUDF(UDF):
    """UDF for performing candidate extraction."""

    def __init__(self, candidate_classes, **kwargs):
        """Initialize the LabelerUDF."""
        self.candidate_classes = (
            candidate_classes
            if isinstance(candidate_classes, (list, tuple))
            else [candidate_classes]
        )
        super(LabelerUDF, self).__init__(**kwargs)

    def _f_gen(self, c):
        """Convert lfs into a generator of id, name, and labels.

        In particular, catch verbose values and convert to integer ones.
        """
        lf_idx = self.candidate_classes.index(c.__class__)
        labels = lambda c: [(c.id, lf.__name__, lf(c)) for lf in self.lfs[lf_idx]]
        for cid, lf_key, label in labels(c):
            # Note: We assume if the LF output is an int, it is already
            # mapped correctly
            if isinstance(label, int):
                yield cid, lf_key, label
            # None is a protected LF output value corresponding to 0,
            # representing LF abstaining
            elif label is None:
                yield cid, lf_key, 0
            elif label in c.values:
                if c.cardinality > 2:
                    yield cid, lf_key, c.values.index(label) + 1
                # Note: Would be nice to not special-case here, but for
                # consistency we leave binary LF range as {-1,0,1}
                else:
                    val = 1 if c.values.index(label) == 0 else -1
                    yield cid, lf_key, val
            else:
                raise ValueError(
                    "Can't parse label value {} for candidate values {}".format(
                        label, c.values
                    )
                )

    def apply(self, doc, split, train, lfs, **kwargs):
        """Extract candidates from the given Context.

        :param doc: A document to process.
        :param split: Which split to use.
        :param train: Whether or not to insert new LabelKeys.
        :param lfs: The list of functions to use to generate labels.
        """
        logger.debug("Document: {}".format(doc))

        if lfs is None:
            raise ValueError("Must provide lfs kwarg.")

        self.lfs = lfs

        # Get all the candidates in this doc that will be featurized
        cands_list = get_cands_list_from_split(
            self.session, self.candidate_classes, doc, split
        )

        label_keys = set()
        for cands in cands_list:
            records = list(get_mapping(cands, self._f_gen, label_keys))
            batch_upsert_records(self.session, Label, records)

        # Insert all Label Keys
        if train:
            add_keys(self.session, LabelKey, label_keys)

        # This return + yield makes a completely empty generator
        return
        yield

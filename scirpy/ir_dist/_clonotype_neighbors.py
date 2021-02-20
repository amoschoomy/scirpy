from multiprocessing import cpu_count
from typing import Union, Sequence
from anndata import AnnData
from scanpy import logging
from scipy.sparse.csr import csr_matrix
from .._compat import Literal
import numpy as np
import scipy.sparse as sp
import itertools
from ._util import DoubleLookupNeighborFinder, BoolSetMask, SetMask
from ..util import _is_na, _is_true
from functools import reduce
from operator import and_, or_
import pandas as pd
from tqdm.contrib.concurrent import process_map

# TODO set tqdm class as specified here: https://github.com/theislab/scanpy/pull/1130/files


class ClonotypeNeighbors:
    def __init__(
        self,
        adata: AnnData,
        *,
        receptor_arms=Literal["VJ", "VDJ", "all", "any"],
        dual_ir=Literal["primary_only", "all", "any"],
        same_v_gene: bool = False,
        within_group: Union[None, Sequence[str]] = None,
        distance_key: str,
        sequence_key: str,
        n_jobs: Union[int, None] = None,
    ):
        """Compute distances between clonotypes"""
        self.same_v_gene = same_v_gene
        self.within_group = within_group
        self.receptor_arms = receptor_arms
        self.dual_ir = dual_ir
        self.distance_dict = adata.uns[distance_key]
        self.sequence_key = sequence_key
        self.n_jobs = n_jobs

        # will be filled in self._prepare
        self.neighbor_finder = None  # instance of DoubleLookupNeighborFinder
        self.clonotypes = None  # pandas data frame with unique receptor configurations
        self.cell_indices = None  # a mapping row index from self.clonotypes -> obs name

        self._receptor_arm_cols = (
            ["VJ", "VDJ"]
            if self.receptor_arms in ["all", "any"]
            else [self.receptor_arms]
        )
        self._dual_ir_cols = ["1"] if self.dual_ir == "primary_only" else ["1", "2"]

        self._cdr3_cols, self._v_gene_cols = list(), list()
        for arm, i in itertools.product(self._receptor_arm_cols, self._dual_ir_cols):
            self._cdr3_cols.append(f"IR_{arm}_{i}_{self.sequence_key}")
            if same_v_gene:
                self._v_gene_cols.append(f"IR_{arm}_{i}_v_gene")

        self._prepare(adata)

    def _prepare(self, adata: AnnData):
        """Initalize the DoubleLookupNeighborFinder and all required lookup tables"""
        start = logging.info("Initializing lookup tables. ")
        self._make_clonotype_table(adata)
        self.neighbor_finder = DoubleLookupNeighborFinder(self.clonotypes)
        self._add_distance_matrices(adata)
        self._add_lookup_tables()
        logging.hint("Done initializing lookup tables.", time=start)

    def _make_clonotype_table(self, adata):
        """Define 'preliminary' clonotypes based identical IR features. """
        if not adata.obs_names.is_unique:
            raise ValueError("Obs names need to be unique!")

        clonotype_cols = self._cdr3_cols + self._v_gene_cols
        if self.within_group is not None:
            clonotype_cols += list(self.within_group)

        obs_filtered = adata.obs.loc[lambda df: _is_true(df["has_ir"]), clonotype_cols]
        # make sure all nans are consistent "nan"
        # This workaround will be made obsolete by #190.
        for col in obs_filtered.columns:
            obs_filtered[col] = obs_filtered[col].astype(str)
            obs_filtered.loc[_is_na(obs_filtered[col]), col] = "nan"

        clonotype_groupby = obs_filtered.groupby(
            clonotype_cols, sort=False, observed=True
        )
        # This only gets the unique_values (the groupby index)
        clonotypes = clonotype_groupby.size().index.to_frame(index=False)

        if clonotypes.shape[0] == 0:
            raise ValueError(
                "Error computing clonotypes. "
                "No cells with IR information found (`adata.obs['has_ir'] == True`)"
            )

        # groupby.indices gets us a (index -> array of row indices) mapping.
        # It doesn't necessarily have the same order as `clonotypes`.
        self.cell_indices = [
            obs_filtered.index[clonotype_groupby.indices.get(ct_tuple, [])].values
            for ct_tuple in clonotypes.itertuples(index=False, name=None)
        ]

        # make 'within group' a single column of tuples (-> only one distance
        # matrix instead of one per column.)
        if self.within_group is not None:
            within_group_col = list(
                clonotypes.loc[:, self.within_group].itertuples(index=False, name=None)
            )
            for tmp_col in self.within_group:
                del clonotypes[tmp_col]
            clonotypes["within_group"] = within_group_col

        # consistency check: there must not be a secondary chain if there is no
        # primary one:
        # TODO add a test for this
        if "2" in self._dual_ir_cols:
            for tmp_arm in self._receptor_arm_cols:
                primary_is_nan = (
                    clonotypes[f"IR_{tmp_arm}_1_{self.sequence_key}"] == "nan"
                )
                secondary_is_nan = (
                    clonotypes[f"IR_{tmp_arm}_2_{self.sequence_key}"] == "nan"
                )
                assert not np.sum(
                    ~secondary_is_nan[primary_is_nan]
                ), "There must not be a secondary chain if there is no primary one"

        self.clonotypes = clonotypes

    def _add_distance_matrices(self, adata):
        """Add all required distance matrices to the DLNF"""
        # sequence distance matrices
        for chain_type in self._receptor_arm_cols:
            self.neighbor_finder.add_distance_matrix(
                name=chain_type,
                distance_matrix=self.distance_dict[chain_type]["distances"],
                labels=self.distance_dict[chain_type]["seqs"],
            )

        if self.same_v_gene:
            # V gene distance matrix (ID mat)
            v_genes = self._unique_values_in_multiple_columns(
                adata.obs, self._v_gene_cols
            )
            self.neighbor_finder.add_distance_matrix(
                "v_gene", sp.identity(len(v_genes), dtype=bool, format="csr"), v_genes  # type: ignore
            )

        if self.within_group is not None:
            within_group_values = np.unique(self.clonotypes["within_group"].values)
            self.neighbor_finder.add_distance_matrix(
                "within_group",
                sp.identity(len(within_group_values), dtype=bool, format="csr"),  # type: ignore
                within_group_values,
            )

    @staticmethod
    def _unique_values_in_multiple_columns(
        df: pd.DataFrame, columns: Sequence[str]
    ) -> np.ndarray:
        return np.unique(np.concatenate([df[c].values for c in columns]))  # type: ignore

    def _add_lookup_tables(self):
        """Add all required lookup tables to the DLNF"""
        for arm, i in itertools.product(self._receptor_arm_cols, self._dual_ir_cols):
            self.neighbor_finder.add_lookup_table(
                f"{arm}_{i}", f"IR_{arm}_{i}_{self.sequence_key}", arm
            )
            if self.same_v_gene:
                self.neighbor_finder.add_lookup_table(
                    f"{arm}_{i}_v_gene", f"IR_{arm}_{i}_v_gene", "v_gene"
                )

        if self.within_group is not None:
            self.neighbor_finder.add_lookup_table(
                "within_group", "within_group", "within_group"
            )

    def compute_distances(self) -> sp.csr_matrix:
        """Compute the distances between clonotypes. `prepare` must have
        been ran previously. Returns a clonotype x clonotype sparse
        distance matrix."""
        start = logging.info(
            "Computing clonotype x clonotype distances. \n"
            "NB: Computation happens in chunks. The progressbar only advances "
            "when a chunk has finished. "
        )  # type: ignore
        n_clonotypes = self.clonotypes.shape[0]
        # dist_rows = process_map(
        #     self._dist_for_clonotype,
        #     range(n_clonotypes),
        #     max_workers=self.n_jobs if self.n_jobs is not None else cpu_count(),
        #     chunksize=2000,
        # )
        # For debugging: single-threaded version
        from tqdm.contrib import tmap

        dist_rows = tmap(self._dist_for_clonotype, range(n_clonotypes))
        dist = sp.vstack(dist_rows)
        dist.eliminate_zeros()
        logging.hint("Done computing clonotype x clonotype distances. ", time=start)
        return dist  # type: ignore

    def _dist_for_clonotype(self, ct_id: int) -> sp.csr_matrix:
        """Compute neighboring clonotypes for a given clonotype.

        Or operations use the min dist of two matching entries.
        And operations use the max dist of two matchin entries.

        The motivation for using the max instead of sum/average is
        that our hypotheis is that a receptor recognizes the same antigen if it
        has a sequence dist < threshold. If we require both receptors to
        match ("and"), the higher one should count.
        """
        res = []
        lookup = dict()
        for tmp_arm in self._receptor_arm_cols:
            chain_ids = (
                [(1, 1)]
                if self.dual_ir == "primary_only"
                else [(1, 1), (2, 2), (1, 2), (2, 1)]
            )
            for c1, c2 in chain_ids:
                lookup[(tmp_arm, c1, c2)] = self.neighbor_finder.lookup(
                    ct_id,
                    f"{tmp_arm}_{c1}",
                    f"{tmp_arm}_{c2}",
                )

        # need to loop through all coordinates that have at least one distance
        has_distance = sp.csr_matrix(reduce(or_, lookup.values()))

        for x in lookup.values():
            x.data = sp.csr_matrix(x.data)

        def make_tmp_res(tmp_arm, c1, c2):
            # ct_col1 = self.clonotypes[f"IR_{tmp_arm}_{c1}_{self.sequence_key}"].values
            ct_col2 = self.clonotypes[f"IR_{tmp_arm}_{c2}_{self.sequence_key}"].values
            return np.array(
                [
                    lookup[(tmp_arm, c1, c2)].data[0, i]
                    if ct_col2[i] != "nan"
                    else np.nan
                    for i in has_distance.indices
                ],
                dtype=float,
            )

        def reduce_and(*args, cols):
            """Take maximum, ignore nans"""
            chain_count = np.sum(
                self.clonotypes.loc[:, cols].iloc[ct_id, :].values == "nan"
            )
            tmp_array = np.vstack(args)
            tmp_array[tmp_array == 0] = np.inf
            same_count_mask = np.sum(np.isnan(tmp_array), axis=0) == chain_count
            tmp_array = np.nanmax(tmp_array, axis=0)
            tmp_array[np.isinf(tmp_array)] = 0
            return np.multiply(tmp_array, same_count_mask)

        def reduce_or(*args, cols=None):
            """Take minimum, ignore 0s and nans"""
            tmp_array = np.vstack(args)
            tmp_array[tmp_array == 0] = np.inf
            tmp_array = np.nanmin(tmp_array, axis=0)
            tmp_array[np.isinf(tmp_array)] = 0
            return tmp_array

        res = []
        for tmp_arm in self._receptor_arm_cols:
            if self.dual_ir == "primary_only":
                tmp_res = make_tmp_res(tmp_arm, 1, 1)
            elif self.dual_ir == "all":
                tmp_res = reduce_or(
                    reduce_and(
                        make_tmp_res(tmp_arm, 1, 1),
                        make_tmp_res(tmp_arm, 2, 2),
                        cols=[f"IR_{tmp_arm}_{c}_{self.sequence_key}" for c in [1, 2]],
                    ),
                    reduce_and(
                        make_tmp_res(tmp_arm, 1, 2),
                        make_tmp_res(tmp_arm, 2, 1),
                        cols=[f"IR_{tmp_arm}_{c}_{self.sequence_key}" for c in [1, 2]],
                    ),
                )
            else:  # "any"
                tmp_res = reduce_or(
                    make_tmp_res(tmp_arm, 1, 1),
                    make_tmp_res(tmp_arm, 1, 2),
                    make_tmp_res(tmp_arm, 2, 2),
                    make_tmp_res(tmp_arm, 2, 1),
                )

            res.append(tmp_res)

        reduce_fun = reduce_and if self.receptor_arms == "all" else reduce_or

        # checking only the chain=1 columns here is enough, as there must not
        # be a secondary chain if there is no first one.
        res = reduce_fun(
            np.vstack(res),
            cols=[f"IR_{arm}_1_{self.sequence_key}" for arm in self._receptor_arm_cols],
        )

        #     if self.dual_ir == "primary_only":
        #         tmp_res = _lookup(1, 1)
        #     elif self.dual_ir == "all":
        #         tmp_res = (_lookup(1, 1) & _lookup(2, 2)) | (
        #             _lookup(1, 2) & _lookup(2, 1)
        #         )
        #     else:  # "any"
        #         tmp_res = _lookup(1, 1) | _lookup(2, 2) | _lookup(1, 2) | _lookup(2, 1)

        #     res.append(tmp_res)

        # operator = and_ if self.receptor_arms == "all" else or_
        # res = reduce(operator, res)

        # TODO within_group + v_genes!
        # if self.within_group is not None:
        #     res = res & self.neighbor_finder.lookup(
        #         ct_id, "within_group", "within_group"
        #     )

        # if it's a bool set masks it corresponds to all nan
        final_res = has_distance.copy()
        final_res.data = res
        return final_res

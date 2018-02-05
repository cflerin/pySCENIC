# -*- coding: utf-8 -*-

from .recovery import recovery, leading_edge
import pandas as pd
import numpy as np
from .utils import load_motif_annotations, COLUMN_NAME_MOTIF_SIMILARITY_QVALUE, COLUMN_NAME_ORTHOLOGOUS_IDENTITY
from itertools import repeat
from .rnkdb import RankingDatabase
from functools import reduce, partial
from dask.multiprocessing import get
from dask import delayed
import multiprocessing
from typing import Type, Sequence, Optional
from .genesig import Regulome, GeneSignature
import math
from cytoolz import compose


COLUMN_NAME_NES = "NES"
COLUMN_NAME_AUC = "AUC"


def module2regulome(db: Type[RankingDatabase], module: Regulome, motif_annotations: pd.DataFrame,
                    rank_threshold: int = 1500, auc_threshold: float = 0.05, nes_threshold=3.0,
                    avgrcc_sample_frac: float = None, weighted_recovery=False) -> Optional[Regulome]:
    """
    Create a regulome for a given ranking database and a co-expression module. If non can be created NoN is returned.

    :param db: The ranking database.
    :param module: The co-expression module.
    :param rank_threshold: The total number of ranked genes to take into account when creating a recovery curve.
    :param auc_threshold: The fraction of the ranked genome to take into account for the calculation of the
        Area Under the recovery Curve.
    :param nes_threshold: The Normalized Enrichment Score (NES) threshold to select enriched features.
    :param avgrcc_sample_frac: The fraction of the features to use for the calculation of the average curve, If None
        then all features are used.
    :param weighted_recovery: Use weights of a gene signature when calculating recovery curves?
    :return: A regulome or None.
    """

    # Load rank of genes from database.
    df = db.load(module)
    features, genes, rankings = df.index.values, df.columns.values, df.values
    weights = np.asarray([module[gene] for gene in genes])

    # Calculate recovery curves, AUC and NES values.
    rccs, aucs = recovery(df, db.total_genes, weights, rank_threshold, auc_threshold)
    ness = (aucs - aucs.mean()) / aucs.std()

    # Keep only features that are enriched, i.e. NES sufficiently high.
    enriched_features_idx = ness >= nes_threshold
    enriched_features = pd.DataFrame(index=pd.MultiIndex.from_tuples(list(zip(repeat(module.transcription_factor),
                                                                              features[enriched_features_idx])),
                                                                     names=["gene_name", "#motif_id"]),
                                     data={COLUMN_NAME_NES: ness[enriched_features_idx],
                                           COLUMN_NAME_AUC: aucs[enriched_features_idx]})
    if len(enriched_features) == 0:
        return None

    # Find motif annotations for enriched features.
    annotated_features = pd.merge(enriched_features, motif_annotations, how="inner", left_index=True, right_index=True)
    if len(annotated_features) == 0:
        return None

    # Calculated leading edge for the remaining enriched features that have annotations.
    if avgrcc_sample_frac is None:
        avgrcc = rccs.mean(axis=0)
        avg2stdrcc =  avgrcc + 2.0 * rccs.std(axis=0)
    else:
        n_features = len(features)
        sample_idx = np.random.randint(0, int(n_features*avgrcc_sample_frac))
        avgrcc = rccs[sample_idx, :].mean(axis=0)
        avg2stdrcc = avgrcc + 2.0 * rccs[sample_idx, :].std(axis=0)

    # Create regulomes for each enriched and annotated feature.
    def score(nes, motif_similarity_qval, orthologuous_identity):
        MAX_VALUE = 100
        score = nes * -math.log(motif_similarity_qval)/MAX_VALUE if not math.isnan(motif_similarity_qval) and motif_similarity_qval != 0.0 else nes
        return score if math.isnan(orthologuous_identity) else score * orthologuous_identity

    regulomes = []
    _module = module if weighted_recovery else module.noweights()
    for (_, row), rcc, ranking in zip(annotated_features.iterrows(), rccs[enriched_features_idx, :], rankings[enriched_features_idx, :]):
        regulomes.append(Regulome(name=module.name,
                                  score=score(row[COLUMN_NAME_NES],
                                              row[COLUMN_NAME_MOTIF_SIMILARITY_QVALUE],
                                              row[COLUMN_NAME_ORTHOLOGOUS_IDENTITY]),
                                  nomenclature=module.nomenclature,
                                  context=module.context.union(frozenset([db.name])),
                                  transcription_factor=module.transcription_factor,
                                  gene2weights=leading_edge(rcc, avg2stdrcc, ranking, genes, _module)))

    # Aggregate these regulomes into a single one using the union operator.
    return reduce(Regulome.union, regulomes)


def derive_regulomes(rnkdbs: Sequence[Type[RankingDatabase]], modules: Sequence[Regulome],
                         motif_annotations_fname: str,
                         rank_threshold: int = 1500, auc_threshold: float = 0.05, nes_threshold=3.0,
                         motif_similarity_fdr: float = 0.001, orthologuous_identity_threshold: float = 0.0,
                         avgrcc_sample_frac: float = None,
                         weighted_recovery=False,
                         num_workers=None) -> Sequence[Regulome]:
    """
    Calculate all regulomes for a given sequence of ranking databases and a sequence of co-expression modules.

    :param rnkdbs: The sequence of databases.
    :param modules: The sequence of modules.
    :param motif_annotations_fname: The name of the file that contains the motif annotations to use.
    :param rank_threshold: The total number of ranked genes to take into account when creating a recovery curve.
    :param auc_threshold: The fraction of the ranked genome to take into account for the calculation of the
        Area Under the recovery Curve.
    :param nes_threshold: The Normalized Enrichment Score (NES) threshold to select enriched features.
    :param motif_similarity_fdr: The maximum False Discovery Rate to find factor annotations for enriched motifs.
    :param orthologuous_identity_threshold: The minimum orthologuous identity to find factor annotations
        for enriched motifs.
    :param avgrcc_sample_frac: The fraction of the features to use for the calculation of the average curve, If None
        then all features are used.
    :param weighted_recovery: Use weights of a gene signature when calculating recovery curves?
    :param num_workers: The number of workers to use for the calculation. None of all available CPUs need to be used.
    :return: A sequence of regulomes.
    """
    motif_annotations = load_motif_annotations(motif_annotations_fname,
                                               motif_similarity_fdr, orthologuous_identity_threshold)
    not_none = lambda r: r is not None
    regulomes = delayed(compose(list, partial(filter, function=not_none)))(
        (delayed(module2regulome)
         (db, gs, motif_annotations, rank_threshold, auc_threshold, nes_threshold, avgrcc_sample_frac)
            for db in rnkdbs for gs in modules))
    return regulomes.compute(get=get, num_workers=num_workers if num_workers else multiprocessing.cpu_count())
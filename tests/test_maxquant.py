from quantmsio.core.maxquant import MaxQuant
from .common import datafile
from unittest import TestCase
from quantmsio.core.psm import Psm
from ddt import data
from ddt import ddt


@ddt
class TestFeatureHandler(TestCase):
    global test_datas
    test_datas = [
        (
            "Maxquant/msms.txt",
            "Maxquant/sdrf.tsv",
        ),
    ]

    @data(*test_datas)
    def test_transform_maxquant(self, test_data):
        evidence_file = datafile(test_data[0])
        sdrf_file = datafile(test_data[1])
        M = MaxQuant(sdrf_file, evidence_file)
        for df in M.iter_batch(chunksize=500000):
            M.transform_psm(df)
            Psm.convert_to_parquet_format(df, M._modifications)
            Psm.transform_parquet(df)

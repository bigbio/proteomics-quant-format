from unittest import TestCase

from ddt import data
from ddt import ddt

from quantmsio.core.query import Parquet

from .common import datafile


@ddt
class TestFeatureHandler(TestCase):
    global test_datas
    test_datas = [
        (
            "/examples/mzml/test.parquet",
            "/examples/mzml/mzml",
        ),
    ]

    @data(*test_datas)
    def test_inject_spectra_msg_to_parquet(self, test_data):
        parquet_file = datafile(test_data[0])
        mz_folder = datafile(test_data[1])
        p = Parquet(parquet_file)
        df = p.get_report_from_database(["test"])
        df["scan_number"] = "1"
        p.inject_spectrum_msg(df, mz_folder)

import pandas as pd
import os
import pyarrow as pa
import pyarrow.parquet as pq
from quantmsio.operate.tools import get_ahocorasick, get_protein_accession
from quantmsio.utils.file_utils import extract_protein_list, save_slice_file, close_file
from quantmsio.core.mztab import MzTab
from quantmsio.core.psm import Psm
from quantmsio.core.sdrf import SDRFHandler
from quantmsio.core.msstats_in import MsstatsIN
from quantmsio.core.common import FEATURE_SCHEMA


class Feature(MzTab):
    def __init__(self, mzTab_path, sdrf_path, msstats_in_path):
        super(Feature, self).__init__(mzTab_path)
        self._msstats_in = msstats_in_path
        self._sdrf_path = sdrf_path
        self._ms_runs = self.extract_ms_runs()
        self._protein_global_qvalue_map = self.get_protein_map()
        self._score_names = self.get_score_names()
        self.experiment_type = SDRFHandler(sdrf_path).get_experiment_type_from_sdrf()
        self._mods_map = self.get_mods_map()
        self._automaton = get_ahocorasick(self._mods_map)

    def extract_psm_msg(self, chunksize=2000000, protein_str=None):
        P = Psm(self.mztab_path)
        pep_dict = P.extract_from_pep(chunksize=chunksize)
        map_dict = {}
        for psm in P.iter_psm_table(chunksize, protein_str):
            for key, df in psm.groupby(["reference_file_name", "peptidoform", "precursor_charge"]):
                df.reset_index(drop=True, inplace=True)
                temp_df = df.iloc[df["posterior_error_probability"].idxmin()]
                if key not in map_dict:
                    map_dict[key] = [None for _ in range(7)]
                pep_value = temp_df["posterior_error_probability"]
                if map_dict[key][0] is None or float(map_dict[key][0]) > float(pep_value):
                    map_dict[key][0] = pep_value
                    map_dict[key][1] = temp_df["calculated_mz"]
                    map_dict[key][2] = temp_df["observed_mz"]
                    map_dict[key][3] = temp_df["mp_accessions"]
                    map_dict[key][4] = temp_df["is_decoy"]
                    map_dict[key][5] = temp_df["additional_scores"]
                    map_dict[key][6] = temp_df["cv_params"]
        return map_dict, pep_dict

    def transform_msstats_in(self, file_num=10, protein_str=None, duckdb_max_memory="16GB", duckdb_threads=4):
        Msstats = MsstatsIN(self._msstats_in, self._sdrf_path, duckdb_max_memory, duckdb_threads)
        for msstats in Msstats.generate_msstats_in(file_num, protein_str):
            yield msstats
        Msstats.destroy_duckdb_database()

    def merge_msstats_and_psm(self, msstats, map_dict):
        map_features = [
            "posterior_error_probability",
            "calculated_mz",
            "observed_mz",
            "mp_accessions",
            "is_decoy",
            "additional_scores",
            "cv_params",
        ]

        def merge_psm(rows, index):
            key = (rows["reference_file_name"], rows["peptidoform"], rows["precursor_charge"])
            if key in map_dict:
                return map_dict[key][index]
            else:
                return None

        for i, feature in enumerate(map_features):
            msstats.loc[:, feature] = msstats[["reference_file_name", "peptidoform", "precursor_charge"]].apply(
                lambda rows: merge_psm(rows, i),
                axis=1,
            )

    def generate_feature(self, file_num=10, protein_str=None, duckdb_max_memory="16GB", duckdb_threads=4):
        for msstats in self.generate_feature_report(file_num, protein_str, duckdb_max_memory, duckdb_threads):
            feature = self.transform_feature(msstats)
            yield feature

    def generate_feature_report(self, file_num=10, protein_str=None, duckdb_max_memory="16GB", duckdb_threads=4):
        map_dict, pep_dict = self.extract_psm_msg(2000000, protein_str)
        for msstats in self.transform_msstats_in(file_num, protein_str, duckdb_max_memory, duckdb_threads):
            self.merge_msstats_and_psm(msstats, map_dict)
            self.add_additional_msg(msstats, pep_dict)
            self.convert_to_parquet_format(msstats)
            yield msstats

    @staticmethod
    def slice(df, partitions):
        cols = df.columns
        if not isinstance(partitions, list):
            raise Exception(f"{partitions} is not a list")
        if len(partitions) == 0:
            raise Exception(f"{partitions} is empty")
        for partion in partitions:
            if partion not in cols:
                raise Exception(f"{partion} does not exist")
        for key, df in df.groupby(partitions):
            yield key, df

    def generate_slice_feature(
        self, partitions, file_num=10, protein_str=None, duckdb_max_memory="16GB", duckdb_threads=4
    ):
        for msstats in self.generate_feature_report(file_num, protein_str, duckdb_max_memory, duckdb_threads):
            for key, df in self.slice(msstats, partitions):
                feature = self.transform_feature(df)
                yield key, feature

    @staticmethod
    def transform_feature(df):
        return pa.Table.from_pandas(df, schema=FEATURE_SCHEMA)

    def write_feature_to_file(
        self, output_path, file_num=10, protein_file=None, duckdb_max_memory="16GB", duckdb_threads=4
    ):
        protein_list = extract_protein_list(protein_file) if protein_file else None
        protein_str = "|".join(protein_list) if protein_list else None
        pqwriter = None
        for feature in self.generate_feature(file_num, protein_str, duckdb_max_memory, duckdb_threads):
            if not pqwriter:
                pqwriter = pq.ParquetWriter(output_path, feature.schema)
            pqwriter.write_table(feature)
        close_file(pqwriter=pqwriter)

    def write_features_to_file(
        self,
        output_folder,
        filename,
        partitions,
        file_num=10,
        protein_file=None,
        duckdb_max_memory="16GB",
        duckdb_threads=4,
    ):
        pqwriters = {}
        protein_list = extract_protein_list(protein_file) if protein_file else None
        protein_str = "|".join(protein_list) if protein_list else None
        for key, feature in self.generate_slice_feature(
            partitions, file_num, protein_str, duckdb_max_memory, duckdb_threads
        ):
            pqwriters = save_slice_file(feature, pqwriters, output_folder, key, filename)
        close_file(pqwriters)

    def generate_best_scan(self, rows, pep_dict):
        key = (rows["peptidoform"], rows["precursor_charge"])
        if key in pep_dict:
            return [pep_dict[key][1], pep_dict[key][2]]
        else:
            return [None, None]

    def add_additional_msg(self, msstats, pep_dict):
        select_mods = list(self._mods_map.keys())
        msstats.loc[:, "pg_global_qvalue"] = msstats["mp_accessions"].map(self._protein_global_qvalue_map)
        msstats[["scan_reference_file_name", "scan"]] = msstats[["peptidoform", "precursor_charge"]].apply(
            lambda rows: self.generate_best_scan(rows, pep_dict), axis=1, result_type="expand"
        )
        msstats[["peptidoform", "modifications"]] = msstats[["peptidoform"]].apply(
            lambda row: self.generate_modifications_details(
                row["peptidoform"], self._mods_map, self._automaton, select_mods
            ),
            axis=1,
            result_type="expand",
        )
        msstats["mp_accessions"] = msstats["mp_accessions"].apply(get_protein_accession)
        msstats.loc[:, "additional_intensities"] = None
        msstats.loc[:, "predicted_rt"] = None
        msstats.loc[:, "gg_accessions"] = None
        msstats.loc[:, "gg_names"] = None
        msstats.loc[:, "rt_start"] = None
        msstats.loc[:, "rt_stop"] = None
        msstats.loc[:, "ion_mobility"] = None
        msstats.loc[:, "start_ion_mobility"] = None
        msstats.loc[:, "stop_ion_mobility"] = None

    @staticmethod
    def convert_to_parquet_format(res):
        res["pg_global_qvalue"] = res["pg_global_qvalue"].astype(float)
        res["unique"] = res["unique"].astype("Int32")
        res["precursor_charge"] = res["precursor_charge"].map(lambda x: None if pd.isna(x) else int(x)).astype("Int32")
        res["calculated_mz"] = res["calculated_mz"].astype(float)
        res["observed_mz"] = res["observed_mz"].astype(float)
        res["posterior_error_probability"] = res["posterior_error_probability"].astype(float)
        res["is_decoy"] = res["is_decoy"].map(lambda x: None if pd.isna(x) else int(x)).astype("Int32")
        res["scan"] = res["scan"].astype(str)
        res["scan_reference_file_name"] = res["scan_reference_file_name"].astype(str)
        if "rt" in res.columns:
            res["rt"] = res["rt"].astype(float)
        else:
            res.loc[:, "rt"] = None

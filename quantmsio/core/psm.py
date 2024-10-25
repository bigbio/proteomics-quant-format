import pyarrow as pa
import pyarrow.parquet as pq
from quantmsio.utils.file_utils import extract_protein_list
from quantmsio.utils.pride_utils import generate_scan_number
from quantmsio.operate.tools import get_ahocorasick
from quantmsio.core.common import PSM_USECOLS, PSM_MAP, PSM_SCHEMA
from quantmsio.core.mztab import MzTab
import pandas as pd

class Psm(MzTab):
    def __init__(self, mzTab_path):
        super(Psm, self).__init__(mzTab_path)
        self._ms_runs = self.extract_ms_runs()
        self._protein_global_qvalue_map = self.get_protein_map()
        self._score_names = self.get_score_names()
        self._mods_map = self.get_mods_map()
        self._automaton = get_ahocorasick(self._mods_map)

    def iter_psm_table(self, chunksize=1000000, protein_str=None):
        for df in self.skip_and_load_csv("PSH", chunksize=chunksize):
            if protein_str:
                df = df[df["accession"].str.contains(f"{protein_str}", na=False)]
            no_cols = set(PSM_USECOLS) - set(df.columns)
            for col in no_cols:
                df.loc[:, col] = None
            df.rename(columns=PSM_MAP, inplace=True)
            df.loc[:, "additional_scores"] = df[list(self._score_names.values())].apply(
            self._genarate_additional_scores, axis=1
            )
            df.loc[:, "reference_file_name"] = df["spectra_ref"].apply(lambda x: self._ms_runs[x[: x.index(":")]])
            yield df

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

    def generate_report(self, chunksize=1000000, protein_str=None):
        for df in self.iter_psm_table(chunksize=chunksize, protein_str=protein_str):
            self.transform_psm(df)
            self.add_addition_msg(df)
            self.convert_to_parquet_format(df)
            df = self.transform_parquet(df)
            yield df

    def transform_psm(self, df):
        select_mods = list(self._mods_map.keys())
        df[["peptidoform", "modifications"]] = df[["peptidoform"]].apply(
            lambda row: self.generate_modifications_details(
                row["peptidoform"], self._mods_map, self._automaton, select_mods),
                axis = 1,
                result_type="expand"
        )
        df.loc[:, "scan"] = df["spectra_ref"].apply(generate_scan_number)
        df.drop(["spectra_ref", "search_engine", "search_engine_score[1]"], inplace=True, axis=1)

    @staticmethod
    def transform_parquet(df):
        return pa.Table.from_pandas(df, schema=PSM_SCHEMA)

    def _genarate_additional_scores(self, cols):
        struct_list = []
        for software, score in self._score_names.items():
            struct = {"name": software, "value": cols[score]}
            struct_list.append(struct)
        return struct_list

    def add_addition_msg(self, df):
        df.loc[:, "cv_params"] = None
        df.loc[:, "predicted_rt"] = None
        df.loc[:, "ion_mobility"] = None
        df.loc[:, "number_peaks"] = None
        df.loc[:, "mz_array"] = None
        df.loc[:, "intensity_array"] = None

    def write_psm_to_file(self, output_path, chunksize=1000000, protein_file=None):
        protein_list = extract_protein_list(protein_file) if protein_file else None
        protein_str = "|".join(protein_list) if protein_list else None
        pqwriter = None
        for p in self.generate_report(chunksize=chunksize, protein_str=protein_str):
            if not pqwriter:
                pqwriter = pq.ParquetWriter(output_path, p.schema)
            pqwriter.write_table(p)
        if pqwriter:
            pqwriter.close()

    @staticmethod
    def convert_to_parquet_format(res):
        res["mp_accessions"] = res["mp_accessions"].str.split(";")
        res["precursor_charge"] = res["precursor_charge"].map(lambda x: None if pd.isna(x) else int(x)).astype("Int32")
        res["calculated_mz"] = res["calculated_mz"].astype(float)
        res["observed_mz"] = res["observed_mz"].astype(float)
        res["posterior_error_probability"] = res["posterior_error_probability"].astype(float)
        res["is_decoy"] = res["is_decoy"].map(lambda x: None if pd.isna(x) else int(x)).astype("Int32")
        res["scan"] = res["scan"].astype(str)
        if "rt" in res.columns:
            res["rt"] = res["rt"].astype(float)
        else:
            res.loc[:, "rt"] = None

#df.loc[:, "pg_global_qvalue"] = df["mp_accessions"].map(self._protein_global_qvalue_map)
#res["pg_global_qvalue"] = res["pg_global_qvalue"].astype(float)
#res["unique"] = res["unique"].astype("Int32")
#res["global_qvalue"] = res["global_qvalue"].astype(float)
import os
import re
import duckdb
from quantms_io.core.feature import FeatureHandler
from quantms_io.core.openms import OpenMSHandler
from quantms_io.core.psm import PSMHandler
import pyarrow as pa
import pyarrow.parquet as pq
from Bio import SeqIO
import ahocorasick

def check_string(re_exp, strings):
    res = re.search(re_exp, strings)
    if res:
        return True
    else:
        return False
    
def map_spectrum_mz(mz_path: str, scan: str, mzml: dict, mzml_directory: str):
    """
    mz_path: mzML file path
    scan: scan number
    mzml: OpenMSHandler object
    """
    if mzml_directory.endswith("/"):
        mz_path = mzml_directory + mz_path + ".mzML"
    else:
        mz_path = mzml_directory + "/" + mz_path + ".mzML"
    mz_array, array_intensity = mzml[mz_path].get_spectrum_from_scan(mz_path, int(scan))
    return mz_array, array_intensity, 0

def fill_start_and_end(row,protein_dict):
    """
    Map seq location from fasta file.
    return: Tuple of location
    """
    start = []
    end = []
    for protein in list(row['protein_accessions']):
        automaton = ahocorasick.Automaton()
        automaton.add_word(row['sequence'],row['sequence'])
        automaton.make_automaton()
        for item in automaton.iter(protein_dict[protein]):
            end.append(item[0])
            start.append(item[0]-len(row['sequence'])+1)
    return start,end

class Parquet:

    def __init__(self, parquet_path: str):
        if os.path.exists(parquet_path):
            self.parquet_db = duckdb.connect()
            self.parquet_db = self.parquet_db.execute(
                "CREATE VIEW parquet_db AS SELECT * FROM parquet_scan('{}')".format(parquet_path))
        else:
            raise FileNotFoundError(f'the file {parquet_path} does not exist.')
    
    def get_report_from_database(self, runs: list):
        """
        This function loads the report from the duckdb database for a group of ms_runs.
        :param runs: A list of ms_runs
        :return: The report
        """
        database = self.parquet_db.sql(
            """
            select * from parquet_db
            where reference_file_name IN {}
            """.format(tuple(runs))
        ) 
        report = database.df()
        return report
    
    def generate_spectrum_msg(self, mzml_directory: str,output_path:str,label:str,partition:str=None,file_num:int=2):
        """
        This function injects the spectral message from the mzml file.
        :param mzml_directory: Mzml folder
        :param output_path: New parquet file path
        :param label: Control file type[feature,psm]
        :param partition: Criteria for splitting files[charge,reference_file_name,none]
        :param file_num: The number of files being processed at the same time(default 2)
        :return: none
        """
        pqwriters = {}
        pqwriter_no_part = None
        references = [reference.split('.')[0] for reference in os.listdir(mzml_directory)]
        ref_list =  [references[i:i+file_num] for i in range(0,len(references), file_num)]
        for refs in ref_list:
            table = self.get_report_from_database(refs)
            mzml = {ref:OpenMSHandler() for ref in refs}
            if label == 'feature':
                table[["mz_array", "intensity_array", "num_peaks"]] = table[
                    ["best_psm_reference_file_name", "best_psm_scan_number"]
                ].apply(
                    lambda x: map_spectrum_mz(
                        x["best_psm_reference_file_name"],
                        x["best_psm_scan_number"],
                        mzml,
                        mzml_directory,
                    ),
                    axis=1,
                    result_type="expand",
                )
                hander = FeatureHandler()
            else:
                table[["mz_array", "intensity_array", "num_peaks"]] = table[
                    ["reference_file_name", "scan_number"]
                ].apply(
                    lambda x: map_spectrum_mz(
                        x["reference_file_name"],
                        x["scan_number"],
                        mzml,
                        mzml_directory,
                    ),
                    axis=1,
                    result_type="expand",
                )
                hander = PSMHandler()
            #save
            if partition == 'charge':
                for key, df in table.groupby(['charge']):
                    parquet_table = pa.Table.from_pandas(df, schema=hander.schema)
                    save_path = output_path.split(".")[0] +"-" + str(key) + '.parquet'
                    if not os.path.exists(save_path):
                        pqwriter = pq.ParquetWriter(save_path, parquet_table.schema)
                        pqwriters[key] = pqwriter
                    pqwriters[key].write_table(parquet_table)
            elif partition == 'reference_file_name':
                for key, df in table.groupby(['reference_file_name']):
                    parquet_table = pa.Table.from_pandas(df, schema=hander.schema)
                    save_path = output_path.split(".")[0] + "-" + str(key) + '.parquet'
                    if not os.path.exists(save_path):
                        pqwriter = pq.ParquetWriter(save_path, parquet_table.schema)
                        pqwriters[key] = pqwriter
                    pqwriters[key].write_table(parquet_table)
            else:
                parquet_table = pa.Table.from_pandas(table, schema=hander.schema)
                if not pqwriter_no_part:
                    pqwriter_no_part = pq.ParquetWriter(output_path, parquet_table.schema)
                pqwriter_no_part.write_table(parquet_table)
        #close f
        if not partition:
            if pqwriter_no_part:
                pqwriter_no_part.close()
        else:
            for pqwriter in pqwriters.values():
                pqwriter.close()

    def generate_start_and_end_from_fasta(self,fasta_path,label,output_path,file_num=50):
        """
        This function injects locations from fasta to parquet file.
        :param fasta_path: Ref fasta file
        :param label: Control file type[feature,psm]
        :oaram output_path: New parquet file path
        :param file_num: The number of files being processed at the same time(default 50)
        :return none
        """
        protein_dict = self.get_protein_dict(fasta_path)
        if label == 'feature':
            hander = FeatureHandler()
        elif label == 'psm':
            hander = PSMHandler()
        pqwriter = None
        references = self.get_unique_references()
        ref_list =  [references[i:i+file_num] for i in range(0,len(references), file_num)]
        for refs in ref_list:
            df = self.get_report_from_database(refs)
            df[['protein_start_positions','protein_end_positions']] = (df[['sequence','protein_accessions']]
                                                                        .apply(lambda row:fill_start_and_end(row,protein_dict),axis=1,result_type="expand"))
            parquet_table = pa.Table.from_pandas(df, schema=hander.schema)
            if not pqwriter:
                pqwriter = pq.ParquetWriter(output_path, parquet_table.schema)
            pqwriter.write_table(parquet_table)
        if pqwriter:
            pqwriter.close()
    
    def get_protein_dict(self,fasta_path):
        '''
        return: protein_map {protein_accession:seq}
        '''
        df = self.parquet_db.sql(f"SELECT DISTINCT protein_accessions FROM parquet_db").df()
        proteins = set()
        for protein_accessions in df['protein_accessions'].tolist():
            proteins.update(set(protein_accessions))
        protein_dict = {}
        for seq in SeqIO.parse(fasta_path, "fasta"):
            p_name = seq.id.split('|')[1]
            if p_name in proteins and p_name not in protein_dict:
                protein_dict[p_name] = str(seq.seq)
        return protein_dict

    def get_unique_references(self):
        """
        return: A list of deduplicated reference
        """
        unique_reference = self.parquet_db.sql(f"SELECT DISTINCT reference_file_name FROM parquet_db").df()

        return unique_reference['reference_file_name'].tolist()


    def get_unique_peptides(self):
        """
        return: A list of deduplicated peptides.
        """
        unique_peps = self.parquet_db.sql(f"SELECT DISTINCT sequence FROM parquet_db").df()

        return unique_peps['sequence'].tolist()

    def get_unique_proteins(self):
        """
        return: A list of deduplicated proteins.
        """

        unique_prts = self.parquet_db.sql(f"SELECT DISTINCT protein_accessions FROM parquet_db").df()

        return unique_prts['protein_accessions'].tolist()

    def query_peptide(self, peptide: str):
        """
        peptide: Peptide that need to be queried.
        return: A DataFrame of all information about query peptide.
        """

        if check_string('^[A-Z]+$', peptide):
            return self.parquet_db.sql(f"SELECT * FROM parquet_db WHERE sequence ='{peptide}'").df()
        else:
            return KeyError('Illegal peptide!')


    def query_protein(self, protein: str):
        """
        protein: Protein that need to be queried.
        return: A DataFrame of all information about query protein.
        """
        if check_string('^[A-Z]+', protein):
            return self.parquet_db.sql(f"SELECT * FROM parquet_db WHERE protein_accessions ='{protein}'").df()
        else:
            return KeyError('Illegal protein!')
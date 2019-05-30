"""
Module "read_merging_16S" is dedicated to merge Illumina (MiSeq) pair-end reads from 16S-rDNA.
"""

import os

tmp_file_path = "temp_check.sh"
with open(tmp_file_path, 'w') as tmp_file:
    tmp_file.write("""#!/bin/bash
for utility in fasta36 blastn blastdbcmd; do
    if [[ -z `which $utility` ]]; then
        echo "Attention! $utility is required to use read merging tool." 
        echo "Please, make sure that $utility is installed on your computer if you eant to use it."
        echo 'If this error still occure although you have installed everything -- make sure that all these programs are added to PATH' 
        echo "Exitting..."
        exit 1
    fi
done; exit 0""")
if os.system("bash {}".format(tmp_file_path)) != 0:
    os.remove(tmp_file_path)
    exit(1)
os.remove(tmp_file_path)

# ===============================  Data   ===============================


blast_rep = "blastn_report.txt"
fasta_rep = "fasta36_report.txt"
query = "query.fasta"
sbjct = "subject.fasta"
ncbi_fmt_db = "REPLACE_DB"
constV3V4_path = "REPLACE_CONST"

QSEQID, SSEQID, PIDENT, LENGTH, MISMATCH, GAPOPEN, QSTART, QEND, SSTART, SEND, EVALUE, BITSCORE, SACC, SSTRAND = range(14)
cmd_for_blastn = """blastn -query {} -db {} -penalty -1 -reward 2 -ungapped \
-outfmt "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore sacc sstrand" \
-out {} -task blastn -max_target_seqs 1""".format(query, ncbi_fmt_db, blast_rep)

draft_cmd_for_blastdbcmd = "blastdbcmd -db {} -entry REPLACE_ME -out {}".format(ncbi_fmt_db, sbjct)

cmd_for_fasta = "fasta36 {} {} -n -f 20 -g 10 -m 8 -3 > {}".format(query, sbjct, fasta_rep)


# Following names seem to be rather common, so it is better to add an undesscore.
_RC_DICT = {
    'A': 'T',
    'T': 'A',
    'C': 'G',
    'G': 'C',
    'U': 'A',   # in this case it does not matter
    'N': 'N'    # just in case
}
_single_nucl_rc = lambda nucl: _RC_DICT[nucl]
_rc = lambda seq: "".join(map(_single_nucl_rc, seq[::-1]))

# We need just it's length stored in RAM
const_V3_V4 = ""
with open(constV3V4_path, 'r') as const_seq_file:
    const_seq_file.readline()
    for line in const_seq_file:
        const_V3_V4 += line.strip()
constV3V4_len = len(const_V3_V4)
del const_V3_V4

# === These constants had been chosen empirically: ===

# This constant is used fir detecting random alignments in the center of reads:
MAX_ALIGN_OFFSET = 40

# Aligner can miss short overlapping region, especially if there are some sequencing enrrors at the end of reads
MIN_OVERLAP = 11


# Constant region should be in center of a merged sequence
MAX_CONST_MID_OFFS = 70

# Constant region should align good enough, since it is constant
# (percent of identity is provided by fasta36, coverage is not and we need to calculate it manually,
# therefore coverage is if fraction form and identity -- in percent form)
MIN_CONST_COV = 0.90
MIN_CONST_IDENT = 80.0

# Maximum credible gap length. Very long gap isn't probable enough.
MAX_CRED_GAP_LEN = 90


curr_merged_seq = ""
curr_merged_qual = ""


# Constants for naive "aligning"
SEED_LEN = MIN_OVERLAP
INDCS = range(SEED_LEN)
MAX_SHIFT = 120
MIN_PIDENT = 0.90


# ===============================  Internal functions  ===============================


def _naive_align(fseq, rseq):
    """
    This function takes the tail of forward read and slides it through the reverse read.
    If sugnificant similarity is found -- return value of shift.
    Otherwise rerurn -1.
    :param fseq: forward read
    :type fseq: str
    :param rseq: reverse read
    :type rseq: str
    :return: int
    """
    seed = fseq[len(fseq)-SEED_LEN-1:]

    for shift in range(MAX_SHIFT):
        score = 0
        for pos in INDCS:
            if seed[pos] == rseq[pos + shift]:
                score += 1

        if score / SEED_LEN > MIN_PIDENT:
            return shift

    return -1


def _al_ag_one_anoth(f_id, fseq, r_id, rseq):
    """
    Internal function.
    Align reads against one another with fasta36.
    :param f_id: id of the forward sequence
    :type f_id: str
    :param fseq: forward sequence itself
    :type fseq: str
    :param r_id: id of the reverse sequence
    :type r_id: str
    :param rseq: reverse sequence itself
    :type rseq: str
    :return: report generated by fasta36
    :return type: list<str> 
    """

    # form query file
    with open(query, 'w') as query_file:
        query_file.write(f_id.replace('@', '>') + '\n')
        query_file.write(fseq)

    # form subject file
    with open(sbjct, 'w') as sbjct_file:
        sbjct_file.write(r_id.replace('@', '>') + '\n')
        sbjct_file.write(rseq)

    # align forward read against reverse read
    os.system(cmd_for_fasta)

    # parse report
    with open(fasta_rep, 'r') as fasta_repf:
        far_report = fasta_repf.readline().strip().split('\t')  # "far" means Forward [read] Against Reverse [read]

    return far_report


def _merge_by_overlap(loffset, overl, fseq, fqual, rseq, rqual):
    """
    Internal function.
    Merge two reads according to their overlapping redion.
    Function leaves nucleotide with higher quality.
    :param loffset: number of nucleotides in forward read from it's beginning to start of the overlapping region
    :type loffset: int
    :param overl: number of nucleotides in the overlapping region
    :type overl: int
    :param fseq: forward read itself
    :type fseq: str
    :param fqual: quality string of the forward read
    :type fqual: str
    :param rseq: reverse read itself
    :type rseq: str
    :param rqual: quality string of the reverse read
    :type rqual: str
    """

    # beginning comes from forward read
    merged_seq = fseq[: loffset]
    merged_qual = fqual[: loffset]

    # "recover" overlapping region depending on quality 
    for i, j in zip(range(loffset, loffset + overl), range(overl)):
        # try:
        qual, nucl = (fqual[i], fseq[i]) if ord(fqual[i]) >= ord(rqual[j]) else (rqual[j], rseq[j])
        # except:
        #     print(i, j)
        #     print(overl, loffset)
        #     print(len(fseq), len(rseq))
        #     exit(0)
        merged_seq += nucl
        merged_qual += qual

    # end comes from reverse read
    merged_seq += rseq[overl :]
    merged_qual += rqual[overl :]

    return (merged_seq, merged_qual)


def _blast_and_align(rseq, r_id):
    """
    Internal function.
    Sequence of actions performed by this function:
    1. Blast forward read against the database.
    2. Find and get reference sequence, against which forward read has aligned with the better score (e. i. the first in list).
    3. Align reverse read agaist this reference sequence.
    4. Return results of these alignments.
    :param rseq: reverse read
    :type rseq: str
    """

    # Assume that query is still the same.
    # Blast forward read.
    os.system(cmd_for_blastn)   # query is still the same
    with open(blast_rep, 'r') as blast_repf:
        # faref means Forward [read] Against REFerence [sequence]
        faref_report = blast_repf.readline().strip().split('\t')

        # get ref sequence
        cmd_for_blastbdcmd = draft_cmd_for_blastdbcmd.replace("REPLACE_ME", faref_report[SACC])
        os.system(cmd_for_blastbdcmd)
        if faref_report[SSTRAND] == "minus":
            with open(sbjct, 'r') as sbjct_file:
                sbjct_seq_id = sbjct_file.readline().strip()
                sbjct_seq = ""
                for line in sbjct_file:
                    sbjct_seq += line.strip()
            sbjct_seq = _rc(sbjct_seq)
            with open(sbjct, 'w') as sbjct_file:
                sbjct_file.write(sbjct_seq_id + '\n')
                sbjct_file.write(sbjct_seq)

        # align reverse read against the reference
        with open(query, 'w') as query_file:
            query_file.write(r_id.replace('@', '>') + '\n')
            query_file.write(rseq)
        os.system(cmd_for_fasta)

        with open(fasta_rep, 'r') as fasta_repf:
             # raref means Reverse [read] Against REFerence [sequence]
            raref_report = fasta_repf.readline().strip().split('\t')

        return faref_report, raref_report


def _search_for_constant_region(loffset, overl, fseq, fqual, rseq, rqual):
    """
    Internal function.
    This function tryes to "merge-by-overlap" forward and reverse reads and searches for
        the consensus sequence of constant region of 16S rRNA gene located between
        V3 and V4 variable regions.
    If there is a constant region there -- consider reads as properly merged and return them.
    Else -- consider them as chimera and return 1.
    :param loffset: number of nucleotides in forward read from it's beginning to start of the overlapping region
    :type loffset: int
    :param overl: number of nucleotides in the overlapping region
    :type overl: int
    :param fseq: forward read itself
    :type fseq: str
    :param fqual: quality string of the forward read
    :type fqual: str
    :param rseq: reverse read itself
    :type rseq: str
    :param rqual: quality string of the reverse read
    :type rqual: str
    :return: True if there is a constant region, otherwise False
    """

    # try to merge
    merged_seq, merged_qual = _merge_by_overlap(loffset, overl, fseq, fqual, rseq, rqual)

    # form query file
    with open(sbjct, 'w') as sbjct_file:
        sbjct_file.write(">presumably_merged_read\n")
        sbjct_file.write(merged_seq)

    # form subject file
    os.system("cat {} > {}".format(constV3V4_path, query))

    # align
    os.system(cmd_for_fasta.replace(" -3", ""))   # in this case we need both strands

    # parse report
    with open(fasta_rep, 'r') as fasta_repf:
        pmer_report = fasta_repf.readline().strip().split('\t') # "pmer" means Presumably MERged [read]

    # check if there are a constant region
    enough_cov = float(pmer_report[LENGTH]) / constV3V4_len > MIN_CONST_COV
    enough_indent = float(pmer_report[PIDENT]) > MIN_CONST_IDENT
    # constant region should be in the center of merged sequence:
    in_the_midl = int(pmer_report[SSTART]) > MAX_CONST_MID_OFFS
    in_the_midr = int(pmer_report[SEND]) > len(merged_seq) - MAX_CONST_MID_OFFS


    if enough_cov and enough_indent and in_the_midl and in_the_midr:
        curr_merged_seq = merged_seq
        curr_merged_qual = merged_qual
        return True
    else:
        # probably here we have a chimera
        return False


def _handle_unforseen_case(f_id, fseq, r_id, rseq):
    """
    Internal function.
    Handle unforseen case and Provide man who uses the program with information 
        about how erroneous reads align one against another.
    :param f_id: id of the forward sequence
    :type f_id: str
    :param fseq: forward sequence itself
    :type fseq: str
    :param r_id: id of the reverse sequence
    :type r_id: str
    :param rseq: reverse sequence itself
    :type rseq: str
    :return: void
    """
    error_report = "error_report.txt"
    print("Unforeseen case! It is my fault. Report it to me -- I will fix it.")
    print("You can get som info about reads caused this crash in the following file:\n\t'{}'"
        .format(os.path.abspath(error_report)))
    with open(error_report, 'w') as errfile:
        errfile.write("Forward read:\n\n" + f_id + '\n')
        errfile.write(fseq + "\n\n" + '~' * 40 + '\n')
        errfile.write("Reverse read:\n\n" + r_id + '\n')
        errfile.write(rseq + "\n\n" + ('~' * 40 + '\n') * 2 + '\n')
        errfile.write("ALIGNMENT (FORWARD READ AGAINST ERVERSE ONE):\n\n")

    # ===  Provide man who uses the program with information about how erroneous reads align one against another  ===
    # form query file
    with open(query, 'w') as query_file:
        query_file.write(f_id.replace('@', '>') + '\n')
        query_file.write(fseq)

    # form subject file
    with open(sbjct, 'w') as sbjct_file:
        sbjct_file.write(r_id.replace('@', '>') + '\n')
        sbjct_file.write(rseq)

    # align forward read against reverse read and append result to error report
    os.system("fasta36 {} {} -n -f 20 -g 10 -3 >> {}".format(query, sbjct, error_report))
    

# ===============================  "Public" functions  ===============================


def del_temp_files():
    """
    Delete temporary files used by functions of these module.
    """
    for file in query, sbjct, blast_rep, fasta_rep:
        if os.path.exists(file):
            os.remove(file)



def merge_reads(fastq_recs):
    """
    The "main" function in this module. Performs whole process of read merging.
    :param fastq_reqs: a dictionary of two fastq-records stored as dictionary of it's fields
    :type fastq_reads: dict<str: dict<str, str>>
    Description of this weird parameter:
    The outer dictionary contains two dictionaries accessable by the following keys: "R1" (forward read), "R2" (reverse read).
    Fields of each record should be accessable by the follosing keys:
    1) "seq_id" (ID of the read)
    2) "seq" (sequence itsef)
    3) "optional_id" (the third line, where '+' is usually written)
    4) "quality_str" (quality string in Phred33)
    # Return values:
    # 0 -- if reads can be merged;
    # 1 -- putative chimera
    # 2 -- too short
    # 3 -- fatal error, unforseen case
    """

    f_id = fastq_recs["R1"]["seq_id"]
    fseq = fastq_recs["R1"]["seq"]
    fqual = fastq_recs["R1"]["quality_str"]
    r_id = fastq_recs["R2"]["seq_id"]
    rseq = _rc(fastq_recs["R2"]["seq"])         # reverse-complement
    rqual = fastq_recs["R2"]["quality_str"][::-1]      # reverse


    # |==== Firstly try to find overlapping region with naive-but-relatively-fast algorithm ====|

    # If reads are too short -- IndexError will be raised and these reads will be considered too short.
    # We do not need these reads anyway.
    try:
        shift = _naive_align(fseq, rseq)
    except:
        print("\tWarning!\nA pair of reads that probably are too short is found and therefore ignored.")
        print("Here are their lenghts:\n forward: {} | reverse: {}".format(len(fseq), len(rseq)))
        return 2

    if shift != -1:
        overl = shift + SEED_LEN
        loffset = len(fseq) - overl - 1

        merged_seq, merged_qual = _merge_by_overlap(loffset, overl, fseq, fqual, rseq, rqual)
        
        globals()["curr_merged_seq"] = merged_seq
        globals()["curr_merged_qual"] = merged_qual

        return 0



    # |==== If it didn't work -- align forward read against reverse read and watch what we've got. ====| 

    far_report = _al_ag_one_anoth(f_id, fseq, r_id, rseq)


    # |==== Check how they have aligned ====|
    
    # Consider normal overlap as:
    # FFFFFFFF----
    # ----RRRRRRRR
    reads_normally_overlap = True   # naive assumption

    # Discard following situation:
    # --FFFFFF
    # RRRRRR--
    too_short = int(far_report[QSTART]) < int(far_report[SSTART])
    too_short = too_short or int(far_report[QEND]) / len(fseq) < int(far_report[SEND]) / len(rseq)

    reads_normally_overlap = reads_normally_overlap and not too_short

    # Catch randomly occured alignment in center of sequences, 
    # e. i. reads don't align against one another in a proper way
    rand_align = len(fseq) - int(far_report[QEND]) > MAX_ALIGN_OFFSET or int(far_report[SSTART]) > MAX_ALIGN_OFFSET
    # resulting conslusion
    reads_normally_overlap = reads_normally_overlap and not rand_align


    # |==== Decide what to do according to "analysis" above. ====|

    # === Ok, reads overlap in the way they should do it. ===
    # FFFFFFFF----
    # ----RRRRRRRR
    if reads_normally_overlap:

        loffset = int(far_report[QSTART]) - int(far_report[SSTART])  # offset from the left
        # in the next line we have 1-based terms, hense I need to substract 1:
        overl = int(far_report[LENGTH]) + int(far_report[SSTART]) + (len(fseq) - int(far_report[QEND])) - 1

        merged_seq, merged_qual = _merge_by_overlap(loffset, overl, fseq, fqual, rseq, rqual)
        globals()["curr_merged_seq"] = merged_seq
        globals()["curr_merged_qual"] = merged_qual

        return 0


    # === Randomly occured alignment in center of sequences, need to blast ===
    elif rand_align:

        # === Blast forward read, align reverse read against the reference ===
        # "faref" means Forward [read] Against REFerence [sequence]
        # "raref" means Reverse [read] Against REFerence [sequence]
        faref_report, raref_report = _blast_and_align(rseq, r_id)

        # Calculate some "features".
        # All these "features" are in coordinates of the reference sequence
        forw_start = int(faref_report[SSTART]) - int(faref_report[QSTART])
        forw_end = forw_start + len(fseq)
        rev_start = int(raref_report[SSTART]) - int(raref_report[QSTART])

        gap = True if forw_end < rev_start else False

        # ===  Handle alignment results ====

        # Overlap region is long enough
        if not gap and forw_end - rev_start > MIN_OVERLAP:
            if forw_start < rev_start:
                # Try to merge them and search for constant region in merged sequence
                loffset = rev_start - forw_start
                overl = forw_end - rev_start 
                reply = _search_for_constant_region(loffset, overl, fseq, fqual, rseq, rqual)
                return 0 if reply else 1
            else:
                # Probably it is a chimera
                return 1


        # Length of the overlapping region is short,
        # but reverse read alignes as they should do it, so let it be.
        elif not gap and forw_end - rev_start <= MIN_OVERLAP:
            
            loffset = rev_start - forw_start
            overl = forw_end - rev_start 

            merged_seq, merged_qual = _merge_by_overlap(loffset, overl, fseq, fqual, rseq, rqual)
            globals()["curr_merged_seq"] = merged_seq
            globals()["curr_merged_qual"] = merged_qual
            return 0
            
        # Here we have a gap. 
        elif gap:
            
            gap_len = rev_start - forw_end
            # Very long gap isn't probable enough.
            if gap_len > MAX_CRED_GAP_LEN:
                return 1
            else:
                # Fill in the gap with 'N'.
                merged_seq = fseq + 'N' * (gap_len - 1) + rseq
                merged_qual = fqual + chr(33) * (gap_len - 1) + rqual   # Illumina uses Phred33

                globals()["curr_merged_seq"] = merged_seq
                globals()["curr_merged_qual"] = merged_qual
                return 0

        # === Unforseen case. This code should not be ran. But if it'll do -- report about it. ===
        _handle_unforseen_case(f_id, fseq, r_id, rseq)
        return 3    

    # Too short sequence, even if is merged. We do not need it.
    elif too_short:
        # --FFFFFFF
        # RRRRRRR--
        return 2

    # === Unforseen case. This code should not be ran. But if it'll do -- report about it. ===
    _handle_unforseen_case(f_id, fseq, r_id, rseq)
    return 3
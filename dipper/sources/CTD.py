import csv
import re
import gzip
import os
import logging
import urllib
import urllib.parse
import urllib.request

from dipper.sources.Source import Source
from dipper.models.Model import Model
from dipper.models.Genotype import Genotype
from dipper.models.Pathway import Pathway
from dipper.models.assoc.G2PAssoc import G2PAssoc
from dipper.models.Reference import Reference
from dipper.models.BiolinkVocabulary import BioLinkVocabulary as blv

LOG = logging.getLogger(__name__)


class CTD(Source):
    """
    The Comparative Toxicogenomics Database (CTD) includes curated data
    describing cross-species chemical–gene/protein interactions and
    chemical– and gene–disease associations to illuminate molecular mechanisms
    underlying variable susceptibility and environmentally influenced diseases.

    Here, we fetch, parse, and convert data from CTD into triples,
    leveraging only the associations based on DIRECT evidence
    (not using the inferred associations).
    We currently process the following associations:
    * chemical-disease
    * gene-pathway
    * gene-disease

    CTD curates relationships between genes and chemicals/diseases with
    marker/mechanism and/or therapeutic.
    Unfortunately, we cannot disambiguate between marker (gene expression) and
    mechanism (causation) for these associations.  Therefore, we are left to
    relate these simply by "marker".

    CTD also pulls in genes and pathway membership from KEGG and REACTOME.
    We create groups of these following the pattern that the specific pathway
    is a subclass of 'cellular process' (a go process), and the gene is
    "involved in" that process.

    For diseases, we preferentially use OMIM identifiers when they can be used
    uniquely over MESH.  Otherwise, we use MESH ids.

    Note that we scrub the following identifiers and their associated data:
    * REACT:REACT_116125 - generic disease class
    * MESH:D004283 - dog diseases
    * MESH:D004195 - disease models, animal
    * MESH:D030342 - genetic diseases, inborn
    * MESH:D040181 - genetic dieases, x-linked
    * MESH:D020022 - genetic predisposition to a disease
    """

    files = {
        'chemical_disease_interactions': {
            'file': 'CTD_chemicals_diseases.tsv.gz',
            'url': 'http://ctdbase.org/reports/CTD_chemicals_diseases.tsv.gz'
        },
        #'gene_pathway': {
        #    'file': 'CTD_genes_pathways.tsv.gz',
        #    'url': 'http://ctdbase.org/reports/CTD_genes_pathways.tsv.gz'
        #},
        #'gene_disease': {
        #    'file': 'CTD_genes_diseases.tsv.gz',
        #    'url': 'http://ctdbase.org/reports/CTD_genes_diseases.tsv.gz'
        #}
    }
    static_files = {
        'publications': {'file': 'CTD_curated_references.tsv'}
    }

    def __init__(self, graph_type, are_bnodes_skolemized):
        super().__init__(
            graph_type,
            are_bnodes_skolemized,
            'ctd',
            ingest_title='Comparative Toxicogenomics Database',
            ingest_url='http://ctdbase.org',
            license_url=None,
            data_rights='http://ctdbase.org/about/legal.jsp'
            # file_handle=None
        )

        if 'gene' not in self.all_test_ids:
            LOG.warning("not configured with gene test ids.")
            self.test_geneids = []
        else:
            self.test_geneids = self.all_test_ids['gene']

        if 'disease' not in self.all_test_ids:
            LOG.warning("not configured with disease test ids.")
            self.test_diseaseids = []
        else:
            self.test_diseaseids = self.all_test_ids['disease']

        self.geno = Genotype(self.graph)
        self.pathway = Pathway(self.graph)

        return

    def fetch(self, is_dl_forced=False):
        """
        Override Source.fetch()
        Fetches resources from CTD using the CTD.files dictionary
        Args:
        :param is_dl_forced (bool): Force download
        Returns:
        :return None
        """
        self.get_files(is_dl_forced)

        self._fetch_disambiguating_assoc()

        # consider creating subsets of the files that
        # only have direct annotations (not inferred)
        return

    def parse(self, limit=None):
        """
        Override Source.parse()
        Parses version and interaction information from CTD
        Args:
        :param limit (int, optional) limit the number of rows processed
        Returns:
        :return None
        """
        if limit is not None:
            LOG.info("Only parsing first %d rows", limit)

        LOG.info("Parsing files...")
        # pub_map = dict()
        # file_path = '/'.join((self.rawdir,
        # self.static_files['publications']['file']))
        # if os.path.exists(file_path) is True:
        #     pub_map = self._parse_publication_file(
        #         self.static_files['publications']['file']
        #     )

        if self.test_only:
            self.test_mode = True

        self.geno = Genotype(self.graph)
        self.pathway = Pathway(self.graph)

        self._parse_ctd_file(
            limit, self.files['chemical_disease_interactions']['file'])

        # Per @cmungall's request, removing gene disease associations
        #self._parse_ctd_file(limit, self.files['gene_pathway']['file'])
        #self._parse_ctd_file(limit, self.files['gene_disease']['file'])
        self._parse_curated_chem_disease(limit)

        LOG.info("Done parsing files.")

        return

    def _parse_ctd_file(self, limit, file):
        """
        Parses files in CTD.files dictionary
        Args:
            :param limit (int): limit the number of rows processed
            :param file (str): file name (must be defined in CTD.file)
        Returns:
            :return None
        """
        row_count = 0
        version_pattern = re.compile(r'^# Report created: (.+)$')
        is_versioned = False
        file_path = '/'.join((self.rawdir, file))
        with gzip.open(file_path, 'rt') as tsvfile:
            reader = csv.reader(tsvfile, delimiter="\t")
            for row in reader:
                # Scan the header lines until we get the version
                # There is no official version sp we are using
                # the upload timestamp instead
                if is_versioned is False:
                    match = re.match(version_pattern, ' '.join(row))
                    if match:
                        version = re.sub(r'\s|:', '-', match.group(1))
                        # TODO convert this timestamp to a proper timestamp
                        self.dataset.setVersion(version)
                        is_versioned = True
                elif re.match(r'^#', ' '.join(row)):
                    pass
                else:
                    row_count += 1
                    if file == self.files[
                            'chemical_disease_interactions']['file']:
                        self._process_interactions(row)
                    elif file == self.files['gene_pathway']['file']:
                        self._process_pathway(row)
                    elif file == self.files['gene_disease']['file']:
                        self._process_disease2gene(row)

                if not self.test_mode and limit is not None and row_count >= limit:
                    break

        return

    def _process_pathway(self, row):
        """
        Process row of CTD data from CTD_genes_pathways.tsv.gz
        and generate triples
        Args:
            :param row (list): row of CTD data
        Returns:
            :return None
        """
        model = Model(self.graph)
        self._check_list_len(row, 4)
        (gene_symbol, gene_id, pathway_name, pathway_id) = row

        if self.test_mode and (int(gene_id) not in self.test_geneids):
            return

        entrez_id = 'NCBIGene:' + gene_id

        pathways_to_scrub = [
            'REACT:REACT_116125',  # disease
            "REACT:REACT_111045",  # developmental biology
            "REACT:REACT_200794",  # Mus musculus biological processes
            "REACT:REACT_13685"]   # neuronal system ?

        if pathway_id in pathways_to_scrub:
            # these are lame "pathways" like generic
            # "disease" and "developmental biology"
            return

        # convert KEGG pathway ids... KEGG:12345 --> KEGG-path:map12345
        if re.match(r'KEGG', pathway_id):
            pathway_id = re.sub(r'KEGG:', 'KEGG-path:map', pathway_id)
        # just in case, add it as a class
        model.addClassToGraph(entrez_id, None,
                              class_category=blv.terms.Gene)

        self.pathway.addPathway(pathway_id, pathway_name)
        self.pathway.addGeneToPathway(entrez_id, pathway_id)

        return

    def _fetch_disambiguating_assoc(self):
        """
        For any of the items in the chemical-disease association file that have
        ambiguous association types we fetch the disambiguated associations
        using the batch query API, and store these in a file. Elsewhere, we can
        loop through the file and create the appropriate associations.

        :return:

        """

        disambig_file = '/'.join(
            (self.rawdir, self.static_files['publications']['file']))
        assoc_file = '/'.join(
            (self.rawdir, self.files['chemical_disease_interactions']['file']))

        # check if there is a local association file,
        # and download if it's dated later than the original intxn file
        if os.path.exists(disambig_file):
            dfile_dt = os.stat(disambig_file)
            afile_dt = os.stat(assoc_file)
            if dfile_dt < afile_dt:
                LOG.info(
                    "Local file date before chem-disease assoc file. "
                    " Downloading...")
            else:
                LOG.info(
                    "Local file date after chem-disease assoc file. "
                    " Skipping download.")
                return

        all_pubs = set()
        dual_evidence = re.compile(r'^marker\/mechanism\|therapeutic$')
        # first get all the unique publications
        with gzip.open(assoc_file, 'rt') as tsvfile:
            reader = csv.reader(tsvfile, delimiter="\t")
            for row in reader:
                if re.match(r'^#', ' '.join(row)):
                    continue
                self._check_list_len(row, 10)
                (chem_name, chem_id, cas_rn, disease_name, disease_id,
                 direct_evidence, inferred_gene_symbol, inference_score,
                 omim_ids, pubmed_ids) = row
                if direct_evidence == '' or not \
                        re.match(dual_evidence, direct_evidence):
                    continue
                if pubmed_ids is not None and pubmed_ids != '':
                    all_pubs.update(set(re.split(r'\|', pubmed_ids)))
        sorted_pubs = sorted(list(all_pubs))

        # now in batches of 4000, we fetch the chemical-disease associations
        batch_size = 4000
        params = {
            'inputType': 'reference',
            'report': 'diseases_curated',
            'format': 'tsv',
            'action': 'Download'
        }

        url = 'http://ctdbase.org/tools/batchQuery.go?q'
        start = 0
        end = min((batch_size, len(all_pubs)))  # get them in batches of 4000

        with open(disambig_file, 'wb') as dmbf:
            while start < len(sorted_pubs):
                params['inputTerms'] = '|'.join(sorted_pubs[start:end])
                # fetch the data from url
                LOG.info(
                    'fetching %d (%d-%d) refs: %s',
                    len(re.split(r'\|', params['inputTerms'])),
                    start, end, params['inputTerms'])
                data = urllib.parse.urlencode(params)
                encoding = 'utf-8'
                binary_data = data.encode(encoding)
                req = urllib.request.Request(url, binary_data)
                resp = urllib.request.urlopen(req)
                dmbf.write(resp.read())
                start = end
                end = min((start + batch_size, len(sorted_pubs)))

        return

    def _process_interactions(self, row):
        """
        Process row of CTD data from CTD_chemicals_diseases.tsv.gz
        and generate triples. Only create associations based on direct evidence
        (not using the inferred-via-gene), and unambiguous relationships.
        (Ambiguous ones will be processed in the sister method using the
        disambiguated file). There are no OMIM ids for diseases in these cases,
        so we associate with only the mesh disease ids.
        Args:
            :param row (list): row of CTD data
        Returns:
            :return None
        """
        model = Model(self.graph)
        self._check_list_len(row, 10)
        (chem_name, chem_id, cas_rn, disease_name, disease_id, direct_evidence,
         inferred_gene_symbol, inference_score, omim_ids, pubmed_ids) = row

        if direct_evidence == '':
            return

        evidence_pattern = re.compile(r'^therapeutic|marker\/mechanism$')
        # dual_evidence = re.compile(r'^marker\/mechanism\|therapeutic$')

        # filter on those diseases that are mapped to omim ids in the test set
        intersect = list(
            set(['OMIM:' + str(i) for i in omim_ids.split('|')] +
                [disease_id]) & set(self.test_diseaseids))
        if self.test_mode and len(intersect) < 1:
            return
        chem_id = 'MESH:' + chem_id
        reference_list = self._process_pubmed_ids(pubmed_ids)
        if re.match(evidence_pattern, direct_evidence):
            rel_id = self.resolve(direct_evidence)
            model.addClassToGraph(chem_id, chem_name,
                                  class_category=blv.terms.ChemicalSubstance)
            model.addClassToGraph(disease_id, None,
                                  class_category=blv.terms.Disease)
            self._make_association(chem_id, disease_id, rel_id, reference_list,
                                   )
        else:
            # there's dual evidence, but haven't mapped the pubs
            pass
            # LOG.debug(
            #   "Dual evidence for %s (%s) and %s (%s)",
            #   chem_name, chem_id, disease_name, disease_id)

        return

    def _process_disease2gene(self, row):
        """
        Here, we process the disease-to-gene associations.
        Note that we ONLY process direct associations
        (not inferred through chemicals).
        Furthermore, we also ONLY process "marker/mechanism" associations.

        We preferentially utilize OMIM identifiers over MESH identifiers
        for disease/phenotype.
        Therefore, if a single OMIM id is listed under the "omim_ids" list,
        we will choose this over any MeSH id that might be listed as
        the disease_id. If multiple OMIM ids are listed in the omim_ids column,
        we toss this for now.
        (Mostly, we are not sure what to do with this information.)

        We also pull in the MeSH labels here (but not OMIM) to ensure that
        we have them (as they may not be brought in separately).
        :param row:
        :return:

        """

        # if self.test_mode:
        # graph = self.testgraph
        # else:
        #     graph = self.graph
        # self._check_list_len(row, 9)
        # geno = Genotype(graph)
        # gu = GraphUtils(curie_map.get())
        model = Model(self.graph)
        (gene_symbol, gene_id, disease_name, disease_id, direct_evidence,
         inference_chemical_name, inference_score, omim_ids, pubmed_ids) = row

        # we only want the direct associations; skipping inferred for now
        if direct_evidence == '' or direct_evidence != 'marker/mechanism':
            return

        # scrub some of the associations...
        # it seems odd to link human genes to the following "diseases"
        diseases_to_scrub = [
            'MESH:D004283',  # dog diseases
            'MESH:D004195',  # disease models, animal
            'MESH:D030342',  # genetic diseases, inborn
            'MESH:D040181',  # genetic dieases, x-linked
            'MESH:D020022']   # genetic predisposition to a disease

        if disease_id in diseases_to_scrub:
            LOG.info(
                "Skipping association between NCBIGene:%s and %s",
                str(gene_id), disease_id)
            return

        intersect = list(
            set(['OMIM:' + str(i) for i in omim_ids.split('|')] +
                [disease_id]) & set(self.test_diseaseids))
        if self.test_mode and (
                int(gene_id) not in self.test_geneids or len(intersect) < 1):
            return

        # there are three kinds of direct evidence:
        # (marker/mechanism | marker/mechanism|therapeutic | therapeutic)
        # we are only using the "marker/mechanism" for now
        # TODO what does it mean for a gene to be therapeutic for disease?
        # a therapeutic target?

        gene_id = 'NCBIGene:' + gene_id

        preferred_disease_id = disease_id
        if omim_ids is not None and omim_ids != '':
            omim_id_list = re.split(r'\|', omim_ids)
            # If there is only one OMIM ID for the Disease ID
            # or in the omim_ids list,
            # use the OMIM ID preferentially over any MeSH ID.
            if re.match(r'OMIM:.*', disease_id):
                if len(omim_id_list) > 1:
                    # the disease ID is an OMIM ID and
                    # there is more than one OMIM entry in omim_ids.
                    # Currently no entries satisfy this condition
                    pass
                elif disease_id != ('OMIM:' + omim_ids):
                    # the disease ID is an OMIM ID and
                    # there is only one non-equiv OMIM entry in omim_ids
                    # we preferentially use the disease_id here
                    LOG.warning(
                        "There may be alternate identifier for %s: %s",
                        disease_id, omim_ids)
                    # TODO: What should be done with the alternate disease IDs?
            else:
                if len(omim_id_list) == 1:
                    # the disease ID is not an OMIM ID
                    # and there is only one OMIM entry in omim_ids.
                    preferred_disease_id = 'OMIM:' + omim_ids
                elif len(omim_id_list) > 1:
                    # This is when the disease ID is not an OMIM ID and
                    # there is more than one OMIM entry in omim_ids.
                    pass

        model.addClassToGraph(gene_id, None)

        # not sure if MESH is getting added separately.
        # adding labels here for good measure
        dlabel = None
        if re.match(r'MESH', preferred_disease_id):
            dlabel = disease_name
        model.addClassToGraph(preferred_disease_id, dlabel)

        # Add the disease to gene relationship.
        rel_id = self.resolve(direct_evidence)
        refs = self._process_pubmed_ids(pubmed_ids)

        self._make_association(gene_id, preferred_disease_id, rel_id, refs)

        return

    def _make_association(self, subject_id, object_id, rel_id, pubmed_ids,
                          subject_category=None, object_category=None):
        """
        Make a reified association given an array of pubmed identifiers.

        Args:
            :param subject_id  id of the subject of the association (gene/chem)
            :param object_id  id of the object of the association (disease)
            :param subject_category a biolink category CURIE for subject
            :param object_category a biolink category CURIE for object
            :param rel_id  relationship id
            :param pubmed_ids an array of pubmed identifiers
        Returns:
            :return None

        """

        # TODO pass in the relevant Assoc class rather than relying on G2P
        assoc = G2PAssoc(self.graph, self.name, subject_id, object_id, rel_id,
                         entity_category=subject_category,
                         phenotype_category=object_category)
        if pubmed_ids is not None and len(pubmed_ids) > 0:
            for pmid in pubmed_ids:
                ref = Reference(
                    self.graph, pmid, self.globaltt['journal article'])
                ref.addRefToGraph()
                assoc.add_source(pmid)
                assoc.add_evidence(self.globaltt['traceable author statement'])

        assoc.add_association_to_graph()
        return

    @staticmethod
    def _process_pubmed_ids(pubmed_ids):
        """
        Take a list of pubmed IDs and add PMID prefix
        Args:
            :param pubmed_ids -  string representing publication
                                 ids seperated by a | symbol
        Returns:
            :return list: Pubmed curies

        """
        if pubmed_ids.strip() == '':
            id_list = []
        else:
            id_list = pubmed_ids.split('|')
        for (i, val) in enumerate(id_list):
            id_list[i] = 'PMID:' + val
        return id_list

    def _parse_curated_chem_disease(self, limit):
        model = Model(self.graph)
        line_counter = 0
        file_path = '/'.join(
            (self.rawdir, self.static_files['publications']['file']))
        with open(file_path, 'r') as tsvfile:
            reader = csv.reader(tsvfile, delimiter="\t")
            for row in reader:
                # catch comment lines
                if re.match(r'^#', ' '.join(row)):
                    continue
                line_counter += 1
                self._check_list_len(row, 10)
                (pub_id, disease_label, disease_id, disease_cat, evidence,
                 chem_label, chem_id, cas_rn, gene_symbol, gene_acc) = row

                if disease_id.strip() == '' or chem_id.strip() == '':
                    continue

                rel_id = self.resolve(evidence)
                chem_id = 'MESH:' + chem_id
                model.addClassToGraph(chem_id, chem_label)
                model.addClassToGraph(disease_id, None)
                if pub_id != '':
                    pub_id = 'PMID:' + pub_id
                    ref = Reference(
                        self.graph, pub_id, ref_type=self.globaltt['journal article'])
                    ref.addRefToGraph()
                    pubids = [pub_id]
                else:
                    pubids = None
                self._make_association(chem_id, disease_id, rel_id, pubids,
                                       subject_category=blv.terms.ChemicalSubstance,
                                       object_category=blv.terms.Disease)

                if not self.test_mode and limit is not None and line_counter >= limit:
                    break
        return

    def getTestSuite(self):
        import unittest
        from tests.test_ctd import CTDTestCase

        test_suite = unittest.TestLoader().loadTestsFromTestCase(CTDTestCase)
        # test_suite.addTests(
        #   unittest.TestLoader().loadTestsFromTestCase(InteractionsTestCase))

        return test_suite

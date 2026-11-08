import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[2]/'database/tools/deploy_rules.py'
SPEC=importlib.util.spec_from_file_location('tested_rules_deploy',PATH)
deploy=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(deploy)


class DeploymentSourceTests(unittest.TestCase):
    def test_version_manifest_and_sources_match_approved_hash(self):
        sources,fingerprint=deploy.sources()
        self.assertEqual(len(sources),2)
        self.assertEqual(fingerprint,'d4d60f5ca35edd6ef7ae0785a0deed9ccad7e955b12019d8b254fc6b4761c939')

    def test_modified_source_and_missing_manifest_rejected(self):
        for mode in ('modify','missing'):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);shutil.copytree(deploy.ROOT/'database/plsql',root/'database/plsql')
                if mode=='modify':
                    path=root/'database/plsql'/deploy.FILES[1];path.write_text(path.read_text().replace('RETURN \'1\'','RETURN \'2\''))
                else:(root/'database/plsql/manifest.json').unlink()
                with patch.object(deploy,'ROOT',root),self.assertRaises(ValueError):deploy.sources()

    def test_transaction_and_dynamic_sql_are_not_package_deployment_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);shutil.copytree(deploy.ROOT/'database/plsql',root/'database/plsql')
            path=root/'database/plsql'/deploy.FILES[1]
            path.write_text(path.read_text().replace('END HRMS_RULES;','COMMIT;\nEND HRMS_RULES;'))
            with patch.object(deploy,'ROOT',root),self.assertRaises(ValueError):deploy.sources()

if __name__=='__main__':unittest.main()

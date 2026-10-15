from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.c4 import cdk_diagrams, terraform_diagrams


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TerraformDiagramTests(unittest.TestCase):
    def test_resources_references_and_ignored_syntax(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "infra/main.tf",
                """\
terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

# resource "aws_s3_bucket" "hidden" {
# }

resource "aws_lambda_function" "api" {
  description = "has a } brace"
  source_code_hash = <<-EOT
    resource "aws_sqs_queue" "hidden" {
    }
  EOT
  s3_bucket = aws_s3_bucket.logs.bucket
}

resource "aws_s3_bucket" "logs" {
  bucket = "super-secret-bucket-name"
}

resource "aws_iam_role" "exec" {
  name = "exec"
}
""",
            )
            _write(root, "infra/terraform.tfvars", 'password = "super-secret-tfvars"\n')

            diagrams = terraform_diagrams(root)

        self.assertEqual([diagram.title for diagram in diagrams], ["infra"])
        mermaid = diagrams[0].mermaid
        self.assertIn('Container(api, "api", "AWS Lambda", "aws_lambda_function")', mermaid)
        self.assertIn('Container(logs, "logs", "Amazon S3", "aws_s3_bucket")', mermaid)
        self.assertIn('Rel(api, logs, "References")', mermaid)
        self.assertNotIn("hidden", mermaid)
        self.assertNotIn("aws_iam_role", mermaid)
        self.assertNotIn("super-secret", mermaid)
        self.assertEqual(diagrams[0].files, ["infra/main.tf"])

    def test_event_source_mapping_becomes_an_edge(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "main.tf",
                """\
resource "aws_sqs_queue" "jobs" {}

resource "aws_lambda_function" "worker" {}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.function_name
}
""",
            )
            mermaid = terraform_diagrams(root)[0].mermaid
        self.assertIn('ContainerQueue(jobs, "jobs", "Amazon SQS", "aws_sqs_queue")', mermaid)
        self.assertIn('Rel(jobs, worker, "lambda event source mapping")', mermaid)
        self.assertNotIn("event_source_mapping", mermaid)

    def test_data_source_is_external(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "main.tf",
                """\
resource "aws_lambda_function" "api" {
  s3_bucket = data.aws_s3_bucket.logs.bucket
}
""",
            )
            mermaid = terraform_diagrams(root)[0].mermaid
        self.assertIn(
            'Container_Ext(logs, "logs", "Amazon S3", "data.aws_s3_bucket")',
            mermaid,
        )
        self.assertIn('Rel(api, logs, "References")', mermaid)

    def test_local_module_is_inlined_and_remote_module_is_one_container(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "infra/main.tf",
                """\
module "worker" {
  source = "./modules/worker"
  bucket = aws_s3_bucket.logs.id
}

resource "aws_s3_bucket" "logs" {}

module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
}

module "iam" {
  source = "./modules/iam"
}
""",
            )
            _write(
                root,
                "infra/modules/worker/main.tf",
                'resource "aws_lambda_function" "fn" {}\n',
            )
            _write(
                root,
                "infra/modules/iam/main.tf",
                'resource "aws_iam_role" "exec" {}\n',
            )

            diagrams = terraform_diagrams(root)

        self.assertEqual([diagram.title for diagram in diagrams], ["infra"])
        mermaid = diagrams[0].mermaid
        self.assertIn(
            'Container(worker_fn, "worker/fn", "AWS Lambda", "aws_lambda_function")',
            mermaid,
        )
        self.assertIn('Rel(worker_fn, logs, "References")', mermaid)
        self.assertIn(
            'Container(vpc, "vpc", "terraform-aws-modules/vpc/aws", "Terraform module")',
            mermaid,
        )
        self.assertIn('Container(iam, "iam", "./modules/iam", "Terraform module")', mermaid)
        self.assertNotIn("aws_iam_role", mermaid)
        self.assertEqual(
            diagrams[0].files,
            [
                "infra/main.tf",
                "infra/modules/iam/main.tf",
                "infra/modules/worker/main.tf",
            ],
        )

    def test_unrecognized_resources_are_named(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "main.tf", 'resource "aws_iam_role" "exec" {}\n')
            diagram = terraform_diagrams(root)[0]
        self.assertEqual(diagram.mermaid, "")
        self.assertEqual(
            diagram.note,
            "_No containers recognized. Resource types: aws_iam_role._",
        )

    def test_terraform_inside_node_modules_is_ignored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "node_modules/pkg/main.tf", 'resource "aws_s3_bucket" "logs" {}\n')
            self.assertEqual(terraform_diagrams(root), [])


class CdkDiagramTests(unittest.TestCase):
    def test_python_constructs_and_tests_are_separated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "cdk.json", '{"app": "python app.py"}\n')
            _write(
                root,
                "app.py",
                """\
bucket = s3.Bucket(self, "Uploads")
fn = aws_lambda.Function(
    self,
    "Api",
    environment={"BUCKET": bucket.bucket_name},
)
""",
            )
            _write(
                root,
                "tests/test_app.py",
                'other = s3.Bucket(self, "ShouldNotAppear")\n',
            )

            diagrams = cdk_diagrams(root)

        self.assertEqual(len(diagrams), 1)
        diagram = diagrams[0]
        self.assertEqual(diagram.title, ".")
        self.assertEqual(diagram.detail, "python app.py")
        self.assertEqual(diagram.files, ["app.py"])
        self.assertIn('Container(Uploads, "Uploads", "Amazon S3", "s3.Bucket")', diagram.mermaid)
        self.assertIn(
            'Container(Api, "Api", "AWS Lambda", "aws_lambda.Function")',
            diagram.mermaid,
        )
        self.assertIn('Rel(Api, Uploads, "References")', diagram.mermaid)
        self.assertNotIn("ShouldNotAppear", diagram.mermaid)

    def test_typescript_comments_are_ignored_and_grants_are_edges(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "service/cdk.json", '{"app": "npx ts-node bin/app.ts"}\n')
            _write(
                root,
                "service/bin/app.ts",
                """\
// new s3.Bucket(this, "Hidden")
const bucket = new s3.Bucket(this, "Uploads");
const fn = new lambda.Function(this, "Api", {
  environment: { BUCKET: bucket.bucketName },
});
bucket.grantRead(fn);
""",
            )
            _write(
                root,
                "service/src/app.test.ts",
                'new s3.Bucket(this, "ShouldNotAppear");\n',
            )

            diagram = cdk_diagrams(root)[0]

        self.assertEqual(diagram.title, "service")
        self.assertEqual(diagram.files, ["service/bin/app.ts"])
        self.assertIn('Rel(Api, Uploads, "References")', diagram.mermaid)
        self.assertIn('Rel(Uploads, Api, "References")', diagram.mermaid)
        self.assertNotIn("Hidden", diagram.mermaid)
        self.assertNotIn("ShouldNotAppear", diagram.mermaid)
        self.assertNotIn("cdk.json", diagram.mermaid)

    def test_bare_classes_need_a_cdk_import_and_strings_are_not_edges(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "cdk.json", '{"app": "npx ts-node bin/app.ts"}\n')
            _write(
                root,
                "bin/app.ts",
                """\
import { Bucket } from "aws-cdk-lib/aws-s3";
const uploads = new Bucket(this, "Uploads");
const rows = new Table(this, "Rows");
const fn = new lambda.Function(this, "Api", {
  description: "not the uploads",
});
""",
            )
            mermaid = cdk_diagrams(root)[0].mermaid
        self.assertIn('Container(Uploads, "Uploads", "Amazon S3", "s3.Bucket")', mermaid)
        self.assertNotIn("Rows", mermaid)
        self.assertNotIn("DynamoDB", mermaid)
        self.assertNotIn("Rel(", mermaid)

    def test_imported_constructs_are_edges_across_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "cdk.json", '{"app": "npx ts-node bin/app.ts"}\n')
            _write(
                root,
                "storage.ts",
                """\
export class Storage {
  constructor() {
    this.bucket = new s3.Bucket(this, "Uploads");
  }
}
export const queue = new sqs.Queue(this, "Jobs");
""",
            )
            _write(
                root,
                "bin/app.ts",
                """\
import { queue } from "../storage";
const fn = new lambda.Function(this, "Api", {
  environment: {
    QUEUE: queue.queueUrl,
    BUCKET: storage.bucket.bucketName,
  },
});
""",
            )
            _write(root, "py/cdk.json", '{"app": "python app.py"}\n')
            _write(root, "py/storage.py", 'bucket = s3.Bucket(self, "Uploads")\n')
            _write(
                root,
                "py/app.py",
                """\
from storage import bucket
fn = aws_lambda.Function(self, "Api", environment={"B": bucket.bucket_name})
""",
            )

            diagrams = {diagram.title: diagram for diagram in cdk_diagrams(root)}

        typescript = diagrams["."].mermaid
        self.assertIn('Rel(Api, Jobs, "References")', typescript)
        self.assertIn('Rel(Api, Uploads, "References")', typescript)
        python = diagrams["py"].mermaid
        self.assertIn('Rel(Api, Uploads, "References")', python)

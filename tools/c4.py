"""C4 container diagrams from Terraform and AWS CDK source."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".idea",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".terraform",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "cdk.out",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "vendor",
        "venv",
    }
)

# kind is container, db, or queue. Supporting resources are omitted on purpose.
_TF: dict[str, tuple[str, str]] = {
    "aws_api_gateway_rest_api": ("container", "Amazon API Gateway"),
    "aws_apigatewayv2_api": ("container", "Amazon API Gateway"),
    "aws_apprunner_service": ("container", "AWS App Runner"),
    "aws_appsync_graphql_api": ("container", "AWS AppSync"),
    "aws_autoscaling_group": ("container", "Amazon EC2 Auto Scaling"),
    "aws_batch_compute_environment": ("container", "AWS Batch"),
    "aws_cloudfront_distribution": ("container", "Amazon CloudFront"),
    "aws_cognito_user_pool": ("container", "Amazon Cognito"),
    "aws_db_instance": ("db", "Amazon RDS"),
    "aws_docdb_cluster": ("db", "Amazon DocumentDB"),
    "aws_dynamodb_table": ("db", "Amazon DynamoDB"),
    "aws_ecs_cluster": ("container", "Amazon ECS"),
    "aws_ecs_service": ("container", "Amazon ECS"),
    "aws_efs_file_system": ("container", "Amazon EFS"),
    "aws_eks_cluster": ("container", "Amazon EKS"),
    "aws_eks_node_group": ("container", "Amazon EKS"),
    "aws_elasticache_cluster": ("db", "Amazon ElastiCache"),
    "aws_elasticache_replication_group": ("db", "Amazon ElastiCache"),
    "aws_elasticsearch_domain": ("db", "Amazon OpenSearch"),
    "aws_elb": ("container", "Elastic Load Balancing"),
    "aws_emr_cluster": ("container", "Amazon EMR"),
    "aws_glue_job": ("container", "AWS Glue"),
    "aws_instance": ("container", "Amazon EC2"),
    "aws_kinesis_stream": ("queue", "Amazon Kinesis"),
    "aws_lambda_function": ("container", "AWS Lambda"),
    "aws_lb": ("container", "Elastic Load Balancing"),
    "aws_mq_broker": ("queue", "Amazon MQ"),
    "aws_msk_cluster": ("queue", "Amazon MSK"),
    "aws_nat_gateway": ("container", "Amazon VPC"),
    "aws_neptune_cluster": ("db", "Amazon Neptune"),
    "aws_opensearch_domain": ("db", "Amazon OpenSearch"),
    "aws_rds_cluster": ("db", "Amazon Aurora"),
    "aws_rds_cluster_instance": ("db", "Amazon Aurora"),
    "aws_redshift_cluster": ("db", "Amazon Redshift"),
    "aws_route53_zone": ("container", "Amazon Route 53"),
    "aws_s3_bucket": ("container", "Amazon S3"),
    "aws_sagemaker_endpoint": ("container", "Amazon SageMaker"),
    "aws_sfn_state_machine": ("container", "AWS Step Functions"),
    "aws_sns_topic": ("queue", "Amazon SNS"),
    "aws_sqs_queue": ("queue", "Amazon SQS"),
    "aws_vpc": ("container", "Amazon VPC"),
    "azurerm_app_service": ("container", "Azure App Service"),
    "azurerm_container_app": ("container", "Azure Container Apps"),
    "azurerm_cosmosdb_account": ("db", "Azure Cosmos DB"),
    "azurerm_function_app": ("container", "Azure Functions"),
    "azurerm_kubernetes_cluster": ("container", "Azure Kubernetes Service"),
    "azurerm_linux_function_app": ("container", "Azure Functions"),
    "azurerm_linux_web_app": ("container", "Azure App Service"),
    "azurerm_mssql_database": ("db", "Azure SQL"),
    "azurerm_mssql_server": ("db", "Azure SQL"),
    "azurerm_mysql_flexible_server": ("db", "Azure Database for MySQL"),
    "azurerm_postgresql_flexible_server": ("db", "Azure Database for PostgreSQL"),
    "azurerm_servicebus_namespace": ("queue", "Azure Service Bus"),
    "azurerm_servicebus_queue": ("queue", "Azure Service Bus"),
    "azurerm_storage_account": ("container", "Azure Storage"),
    "azurerm_storage_queue": ("queue", "Azure Storage Queue"),
    "azurerm_virtual_network": ("container", "Azure Virtual Network"),
    "azurerm_windows_function_app": ("container", "Azure Functions"),
    "azurerm_windows_web_app": ("container", "Azure App Service"),
    "google_cloud_run_service": ("container", "Cloud Run"),
    "google_cloud_run_v2_service": ("container", "Cloud Run"),
    "google_cloudfunctions2_function": ("container", "Cloud Functions"),
    "google_cloudfunctions_function": ("container", "Cloud Functions"),
    "google_compute_instance": ("container", "Compute Engine"),
    "google_container_cluster": ("container", "Google Kubernetes Engine"),
    "google_pubsub_subscription": ("queue", "Pub/Sub"),
    "google_pubsub_topic": ("queue", "Pub/Sub"),
    "google_redis_instance": ("db", "Memorystore"),
    "google_sql_database_instance": ("db", "Cloud SQL"),
    "google_storage_bucket": ("container", "Cloud Storage"),
}

_CDK: dict[tuple[str, str], tuple[str, str]] = {
    ("", "ApplicationLoadBalancedFargateService"): ("container", "Amazon ECS"),
    ("", "ApplicationLoadBalancer"): ("container", "Elastic Load Balancing"),
    ("", "Bucket"): ("container", "Amazon S3"),
    ("", "DatabaseCluster"): ("db", "Amazon Aurora"),
    ("", "DatabaseInstance"): ("db", "Amazon RDS"),
    ("", "Distribution"): ("container", "Amazon CloudFront"),
    ("", "DockerImageFunction"): ("container", "AWS Lambda"),
    ("", "Ec2Service"): ("container", "Amazon ECS"),
    ("", "FargateService"): ("container", "Amazon ECS"),
    ("", "FileSystem"): ("container", "Amazon EFS"),
    ("", "HostedZone"): ("container", "Amazon Route 53"),
    ("", "HttpApi"): ("container", "Amazon API Gateway"),
    ("", "LambdaRestApi"): ("container", "Amazon API Gateway"),
    ("", "NetworkLoadBalancer"): ("container", "Elastic Load Balancing"),
    ("", "NodejsFunction"): ("container", "AWS Lambda"),
    ("", "PythonFunction"): ("container", "AWS Lambda"),
    ("", "Queue"): ("queue", "Amazon SQS"),
    ("", "RestApi"): ("container", "Amazon API Gateway"),
    ("", "ServerlessCluster"): ("db", "Amazon Aurora"),
    ("", "StateMachine"): ("container", "AWS Step Functions"),
    ("", "Table"): ("db", "Amazon DynamoDB"),
    ("", "UserPool"): ("container", "Amazon Cognito"),
    ("", "Vpc"): ("container", "Amazon VPC"),
    ("apigateway", "LambdaRestApi"): ("container", "Amazon API Gateway"),
    ("apigateway", "RestApi"): ("container", "Amazon API Gateway"),
    ("apigatewayv2", "HttpApi"): ("container", "Amazon API Gateway"),
    ("aws_apigateway", "LambdaRestApi"): ("container", "Amazon API Gateway"),
    ("aws_apigateway", "RestApi"): ("container", "Amazon API Gateway"),
    ("aws_apigatewayv2", "HttpApi"): ("container", "Amazon API Gateway"),
    ("aws_cloudfront", "Distribution"): ("container", "Amazon CloudFront"),
    ("aws_cognito", "UserPool"): ("container", "Amazon Cognito"),
    ("aws_dynamodb", "Table"): ("db", "Amazon DynamoDB"),
    ("aws_ec2", "Instance"): ("container", "Amazon EC2"),
    ("aws_ec2", "Vpc"): ("container", "Amazon VPC"),
    ("aws_ecs", "Cluster"): ("container", "Amazon ECS"),
    ("aws_ecs", "Ec2Service"): ("container", "Amazon ECS"),
    ("aws_ecs", "FargateService"): ("container", "Amazon ECS"),
    ("aws_efs", "FileSystem"): ("container", "Amazon EFS"),
    ("aws_eks", "Cluster"): ("container", "Amazon EKS"),
    ("aws_eks", "Nodegroup"): ("container", "Amazon EKS"),
    ("aws_kinesis", "Stream"): ("queue", "Amazon Kinesis"),
    ("aws_lambda", "DockerImageFunction"): ("container", "AWS Lambda"),
    ("aws_lambda", "Function"): ("container", "AWS Lambda"),
    ("aws_lambda", "NodejsFunction"): ("container", "AWS Lambda"),
    ("aws_lambda", "PythonFunction"): ("container", "AWS Lambda"),
    ("aws_opensearch", "Domain"): ("db", "Amazon OpenSearch"),
    ("aws_rds", "DatabaseCluster"): ("db", "Amazon Aurora"),
    ("aws_rds", "DatabaseInstance"): ("db", "Amazon RDS"),
    ("aws_rds", "ServerlessCluster"): ("db", "Amazon Aurora"),
    ("aws_route53", "HostedZone"): ("container", "Amazon Route 53"),
    ("aws_s3", "Bucket"): ("container", "Amazon S3"),
    ("aws_sns", "Topic"): ("queue", "Amazon SNS"),
    ("aws_sqs", "Queue"): ("queue", "Amazon SQS"),
    ("aws_stepfunctions", "StateMachine"): ("container", "AWS Step Functions"),
    ("cloudfront", "Distribution"): ("container", "Amazon CloudFront"),
    ("cognito", "UserPool"): ("container", "Amazon Cognito"),
    ("dynamodb", "Table"): ("db", "Amazon DynamoDB"),
    ("ec2", "Instance"): ("container", "Amazon EC2"),
    ("ec2", "Vpc"): ("container", "Amazon VPC"),
    ("ecs", "Cluster"): ("container", "Amazon ECS"),
    ("ecs", "Ec2Service"): ("container", "Amazon ECS"),
    ("ecs", "FargateService"): ("container", "Amazon ECS"),
    ("ecs_patterns", "ApplicationLoadBalancedFargateService"): ("container", "Amazon ECS"),
    ("efs", "FileSystem"): ("container", "Amazon EFS"),
    ("eks", "Cluster"): ("container", "Amazon EKS"),
    ("eks", "Nodegroup"): ("container", "Amazon EKS"),
    ("elasticloadbalancingv2", "ApplicationLoadBalancer"): (
        "container",
        "Elastic Load Balancing",
    ),
    ("elasticloadbalancingv2", "NetworkLoadBalancer"): (
        "container",
        "Elastic Load Balancing",
    ),
    ("kinesis", "Stream"): ("queue", "Amazon Kinesis"),
    ("lambda", "DockerImageFunction"): ("container", "AWS Lambda"),
    ("lambda", "Function"): ("container", "AWS Lambda"),
    ("lambda", "NodejsFunction"): ("container", "AWS Lambda"),
    ("lambda", "PythonFunction"): ("container", "AWS Lambda"),
    ("lambda_", "DockerImageFunction"): ("container", "AWS Lambda"),
    ("lambda_", "Function"): ("container", "AWS Lambda"),
    ("lambda_", "NodejsFunction"): ("container", "AWS Lambda"),
    ("lambda_", "PythonFunction"): ("container", "AWS Lambda"),
    ("opensearch", "Domain"): ("db", "Amazon OpenSearch"),
    ("rds", "DatabaseCluster"): ("db", "Amazon Aurora"),
    ("rds", "DatabaseInstance"): ("db", "Amazon RDS"),
    ("rds", "ServerlessCluster"): ("db", "Amazon Aurora"),
    ("route53", "HostedZone"): ("container", "Amazon Route 53"),
    ("s3", "Bucket"): ("container", "Amazon S3"),
    ("sns", "Topic"): ("queue", "Amazon SNS"),
    ("sqs", "Queue"): ("queue", "Amazon SQS"),
    ("stepfunctions", "StateMachine"): ("container", "AWS Step Functions"),
}

_TWO_STRINGS = {"resource", "data"}
_ONE_STRING = {"module", "variable", "output", "provider"}
_NO_STRINGS = {"locals", "terraform", "moved", "import", "check", "removed"}
_SKIP_REF_TYPES = {"var", "local", "path", "each", "self", "count", "terraform", "module", "data"}
_DATA_REF = re.compile(r"\bdata\.([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_MODULE_REF = re.compile(r"\bmodule\.([A-Za-z_][A-Za-z0-9_]*)")
_RESOURCE_REF = re.compile(r"\b([a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_SOURCE = re.compile(r'(?m)^\s*source\s*=\s*"([^"]*)"')
_CALL = re.compile(
    r"(?:(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)?"
    r"(?:new\s+)?"
    r"(?P<chain>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\("
)
_RESERVED = {
    "boundary",
    "container",
    "edge",
    "node",
    "person",
    "rel",
    "system",
    "title",
}
_KIND = {
    "container": "Container",
    "container_ext": "Container_Ext",
    "db": "ContainerDb",
    "db_ext": "ContainerDb_Ext",
    "queue": "ContainerQueue",
    "queue_ext": "ContainerQueue_Ext",
}
_FILE_LIMIT = 1_000_000


@dataclass
class Diagram:
    title: str
    files: list[str]
    mermaid: str
    note: str
    detail: str = ""


@dataclass
class _Block:
    kind: str
    type_name: str
    name: str
    body: str


@dataclass
class _Item:
    key: str
    type_name: str
    name: str
    label: str
    technology: str
    description: str
    kind: str
    include: bool
    refs: list[str] = field(default_factory=list)


@dataclass
class _Node:
    key: str
    label: str
    kind: str
    technology: str
    description: str
    alias: str = ""


@dataclass
class _Edge:
    src: str
    dst: str
    label: str


class _Hcl:
    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.i = 0

    def blocks(self) -> list[_Block]:
        found: list[_Block] = []
        while self.i < self.n:
            start = self.i
            self._skip_trivia()
            if self.i >= self.n:
                break
            ident = self._read_ident()
            if ident in _TWO_STRINGS or ident in _ONE_STRING or ident in _NO_STRINGS:
                block = self._read_block(ident)
                if block is not None and block.kind in {"resource", "module"}:
                    found.append(block)
                continue
            if self.i == start:
                self.i += 1
        return found

    def _read_block(self, ident: str) -> _Block | None:
        mark = self.i
        if ident in _TWO_STRINGS:
            type_name = self._read_string()
            name = self._read_string()
            body = self._read_body()
            if type_name is None or name is None or body is None:
                if self.i == mark:
                    self.i += 1
                return None
            kind = "resource" if ident == "resource" else "skip"
            return _Block(kind, type_name, name, body)
        if ident in _ONE_STRING:
            name = self._read_string()
            body = self._read_body()
            if name is None or body is None:
                if self.i == mark:
                    self.i += 1
                return None
            kind = "module" if ident == "module" else "skip"
            return _Block(kind, "", name, body)
        body = self._read_body()
        if body is None and self.i == mark:
            self.i += 1
        return None

    def _read_string(self) -> str | None:
        self._skip_trivia()
        if self._peek() != '"':
            return None
        start = self.i
        self._skip_quoted()
        raw = self.text[start : self.i]
        if len(raw) < 2 or not raw.endswith('"'):
            return None
        return raw[1:-1]

    def _read_body(self) -> str | None:
        self._skip_trivia()
        if self._peek() != "{":
            return None
        body_start = self.i + 1
        depth = 1
        self.i += 1
        while self.i < self.n and depth:
            before = self.i
            self._skip_trivia()
            if self.i != before:
                continue
            if self._peek() == '"':
                self._skip_quoted()
                continue
            if self._starts("<<") and self._skip_heredoc():
                continue
            char = self._peek()
            if char == "{":
                depth += 1
                self.i += 1
                continue
            if char == "}":
                depth -= 1
                if depth == 0:
                    body = self.text[body_start : self.i]
                    self.i += 1
                    return body
                self.i += 1
                continue
            self.i += 1
        return self.text[body_start : self.i]

    def _skip_trivia(self) -> None:
        while self.i < self.n:
            char = self._peek()
            if char in " \t\r\n":
                self.i += 1
                continue
            if self._starts("//") or char == "#":
                self._skip_line()
                continue
            if self._starts("/*"):
                self._skip_block_comment()
                continue
            break

    def _skip_line(self) -> None:
        while self.i < self.n and self._peek() != "\n":
            self.i += 1

    def _skip_block_comment(self) -> None:
        self.i += 2
        while self.i < self.n and not self._starts("*/"):
            self.i += 1
        if self._starts("*/"):
            self.i += 2

    def _skip_quoted(self) -> None:
        if self._peek() != '"':
            return
        self.i += 1
        while self.i < self.n:
            if self._peek() == "\\":
                self.i += 2
                continue
            if self._peek() == '"':
                self.i += 1
                return
            self.i += 1

    def _skip_heredoc(self) -> bool:
        if not self._starts("<<"):
            return False
        j = self.i + 2
        if j < self.n and self.text[j] == "-":
            j += 1
        quote = ""
        if j < self.n and self.text[j] in "\"'":
            quote = self.text[j]
            j += 1
        marker_start = j
        while j < self.n and self.text[j] not in " \t\r\n":
            if quote and self.text[j] == quote:
                break
            j += 1
        marker = self.text[marker_start:j]
        if not marker or not re.fullmatch(r"[A-Za-z0-9_]+", marker):
            return False
        if quote:
            if j >= self.n or self.text[j] != quote:
                return False
            j += 1
        while j < self.n and self.text[j] != "\n":
            j += 1
        if j < self.n:
            j += 1
        indented = self.text.startswith("<<-", self.i)
        while j < self.n:
            line_end = self.text.find("\n", j)
            if line_end == -1:
                line_end = self.n
            line = self.text[j:line_end].rstrip("\r")
            check = line.strip() if indented else line
            if check == marker:
                self.i = line_end + 1 if line_end < self.n else self.n
                return True
            j = line_end + 1 if line_end < self.n else self.n
        self.i = self.n
        return True

    def _read_ident(self) -> str:
        if not (self._peek().isalpha() or self._peek() == "_"):
            return ""
        start = self.i
        self.i += 1
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self.i += 1
        return self.text[start : self.i]

    def _peek(self) -> str:
        return self.text[self.i] if self.i < self.n else ""

    def _starts(self, value: str) -> bool:
        return self.text.startswith(value, self.i)

    def searchable(self) -> list[str]:
        pieces: list[str] = []
        code_start = 0
        self.i = 0
        while self.i < self.n:
            if self._peek() in " \t\r\n":
                self.i += 1
                continue
            if self._starts("//") or self._peek() == "#":
                if self.i > code_start:
                    pieces.append(self.text[code_start : self.i])
                self._skip_line()
                code_start = self.i
                continue
            if self._starts("/*"):
                if self.i > code_start:
                    pieces.append(self.text[code_start : self.i])
                self._skip_block_comment()
                code_start = self.i
                continue
            if self._peek() == '"':
                if self.i > code_start:
                    pieces.append(self.text[code_start : self.i])
                content = self._read_string() or ""
                pieces.append(content)
                code_start = self.i
                continue
            if self._starts("<<"):
                heredoc_at = self.i
                if self._skip_heredoc():
                    if heredoc_at > code_start:
                        pieces.append(self.text[code_start:heredoc_at])
                    code_start = self.i
                    continue
            self.i += 1
        if self.i > code_start:
            pieces.append(self.text[code_start : self.i])
        return pieces


def terraform_diagrams(repo: Path) -> list[Diagram]:
    if not repo.is_dir():
        return []
    targets = _module_targets(repo)
    diagrams: list[Diagram] = []
    for directory in _tf_directories(repo):
        if directory in targets:
            continue
        files: list[Path] = []
        items = _parse_dir(directory, repo, "", "", (), files)
        rel_files = sorted({_rel(repo, path) for path in files})
        diagrams.append(_to_diagram(_title(repo, directory), rel_files, items))
    diagrams.sort(key=lambda diagram: diagram.title)
    return diagrams


def cdk_diagrams(repo: Path) -> list[Diagram]:
    if not repo.is_dir():
        return []
    diagrams: list[Diagram] = []
    for app in _cdk_apps(repo):
        nodes: list[_Node] = []
        edges: list[_Edge] = []
        files: list[Path] = []
        for path in _cdk_sources(app):
            text = _read_text(path, _FILE_LIMIT)
            if text is None:
                continue
            files.append(path)
            file_nodes, file_edges = _cdk_file(path, text)
            nodes.extend(file_nodes)
            edges.extend(file_edges)
        detail = _cdk_app_command(app / "cdk.json")
        rel_files = sorted({_rel(repo, path) for path in files})
        diagrams.append(_to_diagram(_title(repo, app), rel_files, [], nodes, edges, detail))
    diagrams.sort(key=lambda diagram: diagram.title)
    return diagrams


def _to_diagram(
    title: str,
    files: list[str],
    items: list[_Item],
    nodes: list[_Node] | None = None,
    edges: list[_Edge] | None = None,
    detail: str = "",
) -> Diagram:
    if nodes is None:
        built, built_edges = _nodes_from_items(items)
    else:
        built = nodes
        built_edges = edges or []
    if not built:
        return Diagram(title, files, "", _empty_note(items), detail)
    mermaid = _mermaid(title, built, built_edges)
    return Diagram(title, files, mermaid, "", detail)


def _nodes_from_items(items: list[_Item]) -> tuple[list[_Node], list[_Edge]]:
    nodes = {
        item.key: _Node(
            key=item.key,
            label=item.label,
            kind=item.kind,
            technology=item.technology,
            description=item.description,
        )
        for item in items
        if item.include
    }
    for item in items:
        for ref in item.refs:
            if ref in nodes or _expand(ref, nodes):
                continue
            external = _ext_from_ref(ref)
            if external is not None:
                nodes[ref] = external
    edges: list[_Edge] = []
    seen: set[tuple[str, str]] = set()

    def add(src: str, dst: str, label: str) -> None:
        if src == dst or src not in nodes or dst not in nodes:
            return
        pair = (src, dst)
        if pair in seen:
            return
        seen.add(pair)
        edges.append(_Edge(src, dst, label))

    for item in items:
        targets: list[str] = []
        for ref in item.refs:
            for key in _expand(ref, nodes):
                if key not in targets:
                    targets.append(key)
        if item.include:
            for target in targets:
                add(item.key, target, "References")
        elif len(targets) >= 2:
            first = targets[0]
            glue = _glue_label(item.type_name)
            for target in targets[1:]:
                add(first, target, glue)
    return list(nodes.values()), edges


def _empty_note(items: list[_Item]) -> str:
    types = sorted({item.type_name for item in items if item.kind == "resource"})
    if not types:
        return "_No containers recognized._"
    shown = types[:20]
    text = ", ".join(shown)
    if len(types) > 20:
        text += ", ..."
    return f"_No containers recognized. Resource types: {text}._"


def _expand(ref: str, nodes: dict[str, _Node]) -> list[str]:
    if ref in nodes:
        return [ref]
    prefix = ref + "."
    return sorted(key for key in nodes if key.startswith(prefix))


def _ext_from_ref(ref: str) -> _Node | None:
    if ref.startswith("data."):
        rest = ref[len("data.") :]
    elif ".data." in ref:
        rest = ref.split(".data.", 1)[1]
    else:
        return None
    type_name, sep, name = rest.partition(".")
    if not sep or not name or "." in name:
        return None
    spec = _TF.get(type_name)
    if spec is None:
        return None
    kind, technology = spec
    return _Node(
        key=ref,
        label=name,
        kind=f"{kind}_ext",
        technology=technology,
        description=f"data.{type_name}",
    )


def _mermaid(title: str, nodes: list[_Node], edges: list[_Edge]) -> str:
    ordered = sorted(nodes, key=lambda node: (node.label.lower(), node.key))
    used: set[str] = set()
    for node in ordered:
        node.alias = _alias(node.label, used)
    by_key = {node.key: node for node in ordered}
    lines = ["C4Container", f"title {title}", ""]
    for node in ordered:
        element = _KIND[node.kind]
        lines.append(
            f"{element}({node.alias}, {_quote(node.label)}, "
            f"{_quote(node.technology)}, {_quote(node.description)})"
        )
    rendered: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        src = by_key.get(edge.src)
        dst = by_key.get(edge.dst)
        if src is None or dst is None or src.alias == dst.alias:
            continue
        pair = (src.alias, dst.alias)
        if pair in seen:
            continue
        seen.add(pair)
        rendered.append((pair[0], pair[1], edge.label))
    for src, dst, label in sorted(rendered):
        lines.append(f"Rel({src}, {dst}, {_quote(label)})")
    return "\n".join(lines)


def _alias(label: str, used: set[str]) -> str:
    alias = re.sub(r"[^A-Za-z0-9_]", "_", label).strip("_")
    if not alias or alias[0].isdigit() or alias.lower() in _RESERVED:
        alias = f"n_{alias}" if alias else "n"
    base = alias
    number = 2
    while alias in used:
        alias = f"{base}_{number}"
        number += 1
    used.add(alias)
    return alias


def _quote(value: str) -> str:
    cleaned = " ".join(value.split()) or "unknown"
    cleaned = cleaned.replace("\\", "\\\\").replace('"', "'")
    return f'"{cleaned}"'


def _glue_label(resource_type: str) -> str:
    for prefix in ("aws_", "google_", "azurerm_"):
        if resource_type.startswith(prefix):
            resource_type = resource_type[len(prefix) :]
            break
    return resource_type.replace("_", " ")


def _parse_dir(
    directory: Path,
    repo: Path,
    scope: str,
    label_prefix: str,
    chain: tuple[Path, ...],
    files: list[Path],
) -> list[_Item]:
    resolved = directory.resolve()
    if resolved in chain:
        return []
    chain = chain + (resolved,)
    items: list[_Item] = []
    for tf in sorted(directory.glob("*.tf")):
        if not tf.is_file() or tf.is_symlink():
            continue
        files.append(tf)
        text = _read_text(tf, _FILE_LIMIT)
        if text is None:
            continue
        for block in _Hcl(text).blocks():
            if block.kind == "resource":
                items.append(_resource_item(block, scope, label_prefix))
            elif block.kind == "module":
                items.extend(_module_items(block, directory, repo, scope, label_prefix, chain, files))
    return items


def _resource_item(block: _Block, scope: str, label_prefix: str) -> _Item:
    key = f"{scope}{block.type_name}.{block.name}"
    refs = [
        ref
        for ref in (_absolute(scope, found) for found in _body_refs(block.body))
        if ref != key
    ]
    spec = _TF.get(block.type_name)
    if spec is None:
        return _Item(
            key=key,
            type_name=block.type_name,
            name=block.name,
            label=f"{label_prefix}{block.name}",
            technology="",
            description=block.type_name,
            kind="resource",
            include=False,
            refs=refs,
        )
    kind, technology = spec
    return _Item(
        key=key,
        type_name=block.type_name,
        name=block.name,
        label=f"{label_prefix}{block.name}",
        technology=technology,
        description=block.type_name,
        kind=kind,
        include=True,
        refs=refs,
    )


def _module_items(
    block: _Block,
    directory: Path,
    repo: Path,
    scope: str,
    label_prefix: str,
    chain: tuple[Path, ...],
    files: list[Path],
) -> list[_Item]:
    source = _module_source(block.body)
    local = _local_module_dir(directory, repo, source) if source else None
    child_scope = f"{scope}module.{block.name}."
    child_label = f"{label_prefix}{block.name}/"
    module_refs = [
        ref
        for ref in (_absolute(scope, found) for found in _body_refs(block.body))
        if ref != f"{scope}module.{block.name}"
    ]
    children: list[_Item] = []
    if local is not None and local.resolve() not in chain:
        children = _parse_dir(local, repo, child_scope, child_label, chain, files)
    included = [child for child in children if child.include and child.key.startswith(child_scope)]
    items = list(children)
    if included:
        external = [ref for ref in module_refs if not ref.startswith(child_scope)]
        for child in included:
            for ref in external:
                if ref not in child.refs:
                    child.refs.append(ref)
        return items
    items.append(
        _Item(
            key=f"{scope}module.{block.name}",
            type_name="module",
            name=block.name,
            label=f"{label_prefix}{block.name}",
            technology=source or "Terraform module",
            description="Terraform module",
            kind="container",
            include=True,
            refs=module_refs,
        )
    )
    return items


def _module_source(body: str) -> str:
    match = _SOURCE.search(body)
    if match is None:
        return ""
    source = match.group(1).strip()
    if "${" in source:
        return ""
    return source


def _local_module_dir(from_dir: Path, repo: Path, source: str) -> Path | None:
    if not source or source.startswith(("git::", "hg::", "s3::", "gcs::", "http://", "https://")):
        return None
    path_part = source.split("//", 1)[0]
    if not path_part.startswith((".", "/")):
        return None
    raw = from_dir / path_part
    if raw.is_symlink():
        return None
    candidate = raw.resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _absolute(scope: str, ref: str) -> str:
    return f"{scope}{ref}"


def _body_refs(body: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for piece in _Hcl(body).searchable():
        for ref in _refs_in(piece):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _refs_in(text: str) -> list[str]:
    spans: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for match in _DATA_REF.finditer(text):
        spans.append((match.start(), match.end()))
        found.append((match.start(), f"data.{match.group(1)}.{match.group(2)}"))
    for match in _MODULE_REF.finditer(text):
        if _overlaps(match.start(), match.end(), spans):
            continue
        spans.append((match.start(), match.end()))
        found.append((match.start(), f"module.{match.group(1)}"))
    for match in _RESOURCE_REF.finditer(text):
        if match.group(1) in _SKIP_REF_TYPES:
            continue
        if _overlaps(match.start(), match.end(), spans):
            continue
        spans.append((match.start(), match.end()))
        found.append((match.start(), f"{match.group(1)}.{match.group(2)}"))
    found.sort()
    return [ref for _, ref in found]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _module_targets(repo: Path) -> set[Path]:
    targets: set[Path] = set()
    for directory in _tf_directories(repo):
        for tf in sorted(directory.glob("*.tf")):
            text = _read_text(tf, _FILE_LIMIT)
            if text is None:
                continue
            for block in _Hcl(text).blocks():
                if block.kind != "module":
                    continue
                source = _module_source(block.body)
                local = _local_module_dir(directory, repo, source) if source else None
                if local is None:
                    continue
                resolved = local.resolve()
                if resolved != directory.resolve():
                    targets.add(resolved)
    return targets


def _tf_directories(repo: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        has_tf = any(
            name.endswith(".tf") and not (current / name).is_symlink() for name in filenames
        )
        if has_tf:
            found.append(current.resolve())
    return sorted(found)


def _cdk_apps(repo: Path) -> list[Path]:
    apps: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        cdk = current / "cdk.json"
        if "cdk.json" in filenames and cdk.is_file() and not cdk.is_symlink():
            apps.append(current)
    return apps


def _cdk_sources(app: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(app):
        current = Path(dirpath)
        if current != app and (current / "cdk.json").is_file():
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in filenames:
            path = current / name
            if _is_cdk_source(app, path):
                files.append(path)
    return sorted(files)


def _is_cdk_source(app: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    name = path.name
    if name.endswith((".d.ts", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return False
    if name.endswith("_test.py") or (name.startswith("test_") and name.endswith(".py")):
        return False
    try:
        relative = path.resolve().relative_to(app.resolve())
    except ValueError:
        return False
    if set(relative.parts) & {"test", "tests", "__tests__"}:
        return False
    return name.endswith((".py", ".ts", ".tsx"))


def _cdk_file(path: Path, text: str) -> tuple[list[_Node], list[_Edge]]:
    python = path.suffix == ".py"
    masked = _mask_code(text, python)
    nodes: list[_Node] = []
    variables: dict[str, str] = {}
    calls: list[tuple[str, str]] = []
    for match in _CALL.finditer(masked):
        looked = _lookup_cdk(match.group("chain"))
        if looked is None:
            continue
        open_paren = match.end() - 1
        if open_paren >= len(text) or text[open_paren] != "(":
            continue
        args, _end = _split_args(text, open_paren, python)
        prefix, class_name, kind, technology = looked
        construct_id = _string_literal(args[1]) if len(args) > 1 else ""
        var = match.group("var") or ""
        label = construct_id or var or class_name
        key = f"{path.as_posix()}:{class_name}:{label}:{match.start()}"
        description = f"{prefix}.{class_name}" if prefix else class_name
        nodes.append(
            _Node(
                key=key,
                label=label,
                kind=kind,
                technology=technology,
                description=description,
            )
        )
        if var and var not in {"self", "this"}:
            variables[var] = key
        calls.append((key, " ".join(args)))
    edges: list[_Edge] = []
    for key, args_text in calls:
        for var, other in variables.items():
            if other == key:
                continue
            if re.search(rf"\b{re.escape(var)}\b", args_text):
                edges.append(_Edge(key, other, "References"))
    for var, key in variables.items():
        for other, other_key in variables.items():
            if var == other:
                continue
            pattern = (
                rf"\b{re.escape(var)}\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*\("
                rf"[^)]*\b{re.escape(other)}\b"
            )
            if re.search(pattern, masked):
                edges.append(_Edge(key, other_key, "References"))
    return nodes, edges


def _lookup_cdk(chain: str) -> tuple[str, str, str, str] | None:
    parts = chain.split(".")
    class_name = parts[-1]
    prefix = parts[-2] if len(parts) > 1 else ""
    found = _CDK.get((prefix, class_name))
    if found is None:
        return None
    kind, technology = found
    return prefix, class_name, kind, technology


def _split_args(text: str, open_paren: int, python: bool) -> tuple[list[str], int]:
    if open_paren >= len(text) or text[open_paren] != "(":
        return [], open_paren
    i = open_paren + 1
    n = len(text)
    depth = 1
    start = i
    args: list[str] = []
    while i < n and depth:
        if text.startswith(("'", '"', "`"), i):
            i = _skip_js_string(text, i)
            continue
        if text.startswith("//", i) or (python and text[i] == "#"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        char = text[i]
        if char in "([{":
            depth += 1
            i += 1
            continue
        if char in ")]}":
            depth -= 1
            if depth == 0 and char == ")":
                args.append(text[start:i])
                return args, i + 1
            i += 1
            continue
        if char == "," and depth == 1:
            args.append(text[start:i])
            i += 1
            start = i
            continue
        i += 1
    args.append(text[start:i])
    return args, i


def _skip_js_string(text: str, start: int) -> int:
    if text.startswith(("'''", '"""'), start):
        quote = text[start : start + 3]
        i = start + 3
        while i < len(text) and not text.startswith(quote, i):
            i += 2 if text[i] == "\\" else 1
        return min(i + 3, len(text))
    quote = text[start]
    i = start + 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" else 1
    return min(i + 1, len(text))


def _string_literal(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return ""


def _mask_code(text: str, python: bool) -> str:
    chars = list(text)
    i = 0
    n = len(chars)
    while i < n:
        if python and chars[i] == "#":
            while i < n and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if not python and i + 1 < n and chars[i] == "/" and chars[i + 1] == "/":
            while i < n and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if not python and i + 1 < n and chars[i] == "/" and chars[i + 1] == "*":
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            while i + 1 < n and not (chars[i] == "*" and chars[i + 1] == "/"):
                if chars[i] != "\n":
                    chars[i] = " "
                i += 1
            if i + 1 < n:
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
            continue
        if python and text.startswith(("'''", '"""'), i):
            quote = text[i : i + 3]
            for offset in range(3):
                chars[i + offset] = " "
            i += 3
            while i < n and not text.startswith(quote, i):
                if chars[i] != "\n":
                    chars[i] = " "
                i += 2 if text[i] == "\\" else 1
            for offset in range(3):
                if i + offset < n:
                    chars[i + offset] = " "
            i += 3
            continue
        if chars[i] in "'\"" or (not python and chars[i] == "`"):
            quote = chars[i]
            chars[i] = " "
            i += 1
            while i < n and chars[i] != quote:
                if chars[i] == "\\" and i + 1 < n:
                    chars[i] = " "
                    i += 1
                if i < n and chars[i] != "\n":
                    chars[i] = " "
                i += 1
            if i < n:
                chars[i] = " "
                i += 1
            continue
        i += 1
    return "".join(chars)


def _cdk_app_command(path: Path) -> str:
    text = _read_text(path, _FILE_LIMIT)
    if text is None:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    app = data.get("app")
    if not isinstance(app, str):
        return ""
    return " ".join(app.split())


def _read_text(path: Path, limit: int) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _rel(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _title(repo: Path, directory: Path) -> str:
    relative = _rel(repo, directory)
    return "." if relative == "." else relative

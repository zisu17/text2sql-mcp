# Text2SQL Graph MCP

Neo4j에 게시된 시맨틱 정의와 물리 계보를 조회하는 MCP 서버입니다. 로컬 stdio와 인증된 원격 Streamable HTTP를 지원합니다. 정의 검증과 그래프 적재는 [semantic-definitions](https://github.com/zisu17/semantic-definitions)가 담당합니다. 이 서버의 도구는 그래프를 수정하거나 원본 DB에 접속하지 않습니다.

## 시작

Python 3.11 이상과 실행 중인 Neo4j가 필요합니다.

```sh
uv sync
export NEO4J_URI='bolt://127.0.0.1:7687'
export NEO4J_USER='neo4j'
export NEO4J_PASSWORD='로컬에서-설정한-암호'
export NEO4J_DATABASE='neo4j'
export GRAPH_TENANT='olist'
export GRAPH_DEFAULT_MODEL='olist_sales'
uv run text2sql-graph-mcp
```

`stdio` MCP 서버이므로 터미널에서 직접 실행하면 입력을 기다립니다. MCP 클라이언트에는 실행 명령 `uv run --directory /절대/경로/text2sql-mcp text2sql-graph-mcp`와 위 환경변수를 등록합니다. 클라이언트마다 등록 파일 위치와 형식은 다릅니다. 비밀번호를 Git에 저장하지 마세요.

## 원격 배포

Railway에서는 `Dockerfile`로 HTTP 앱을 실행하고 Neo4j AuraDB에 TLS로 접속합니다. `/mcp`는 별도 Bearer 토큰이 필요하며 `/healthz`는 배포 상태 확인용입니다. 기존 stdio 실행 명령은 그대로 사용할 수 있습니다. 환경변수 설정과 배포 후 확인 절차는 [Railway 배포 문서](docs/railway.md)에 있습니다.

## 조회 도구

| 도구 | 기능 |
|---|---|
| `graph_list_models` | 활성 모델 목록 |
| `graph_search_definitions` | 데이터셋·필드·지표·개념 검색 |
| `graph_get_dataset` | 데이터셋의 필드·지표·물리 소스 |
| `graph_get_metric_lineage` | 지표의 값 출처와 행 집합 의존성 |
| `graph_get_column_impact` | 물리 컬럼 변경 영향 후보 |
| `graph_list_join_rules` | 선언된 조인 규칙과 상태 |
| `graph_get_concept_mapping` | 개념에 명시적으로 매핑된 필드 |

모든 정의 조회는 `ActiveVersion.version_id`와 `POINTS_TO`가 일치하는 현재 버전으로 한정됩니다. 물리 컬럼은 `source_id`, `catalog`, `schema`, `table`, `column`으로 식별합니다. `row_set_candidate`는 값 출처가 아니라 SQL 소스의 조인·필터 등에 따른 후보 영향입니다.

상세한 도구 계약과 구현 경계는 [MCP 설계 문서](docs/graphdb-mcp-design.md)에 있습니다. `graphdb/load_original.py`는 최초 GraphDB 설계를 재현하는 별도 코드이며 MCP 서버에서 사용하지 않습니다.

## 확인

```sh
uv run python -m unittest discover -s tests -v
```

로컬 그래프 통합 테스트는 `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`를 설정한 뒤 `GRAPH_INTEGRATION_TEST=1`을 추가하면 실행됩니다.

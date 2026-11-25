# Text2SQL Graph MCP

Neo4j에 게시된 시맨틱 정의와 물리 계보를 조회하는 MCP 서버입니다. 로컬 stdio와 인증된 원격 Streamable HTTP를 지원합니다. 정의 검증과 그래프 적재는 [semantic-definitions](https://github.com/zisu17/semantic-definitions)가 담당합니다. 이 서버의 도구는 그래프를 수정하거나 원본 DB에 접속하지 않습니다.

## 문서 안내

| 문서 | 읽을 때 |
| --- | --- |
| 이 README | 서버 실행, 도구 목록, 기본 테스트를 확인할 때 |
| [Graph MCP 조회 계약](docs/graphdb-mcp-design.md) | 활성 버전과 도구별 반환·계보 해석을 확인할 때 |
| [Railway 운영](docs/railway.md) | 원격 인증, 이미지 배포, 운영 점검을 할 때 |
| [계보 그래프 v2 설계안](docs/semantic-lineage-graph-v2.md) | 현재 게시 스키마와 다른 후속 설계안을 검토할 때 |
| [최초 GraphDB 설계 재현](graphdb/README.md) | 초기 설계의 별도 적재 코드와 결과를 재현할 때 |

현재 게시 스키마의 기준은 [semantic-definitions의 GraphDB 문서](https://github.com/zisu17/semantic-definitions/blob/main/docs/graphdb.md)입니다. v2 문서와 최초 설계 재현은 운영 절차가 아닙니다.

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
| `graph_search_definitions` | 데이터셋·필드·지표 검색. 전문 검색 인덱스의 BM25 관련도 순(참조명·이름·동의어가 정확히 같은 항목이 먼저) |
| `graph_get_dataset` | 데이터셋의 필드·지표·물리 소스 후보 |
| `graph_get_metric_lineage` | 지표의 값 출처와 행 집합 의존성 |
| `graph_get_column_impact` | 물리 컬럼 변경 영향 후보 |
| `graph_list_join_rules` | 선언된 조인 규칙과 상태 |
| `graph_find_join_path` | 데이터셋 사이의 승인된 최단 조인 경로. 없으면 승인 전 연결로 가는 경로도 함께 알려 준다. 안내용이다 |

정의 검색은 기본으로 후보 1건을 반환합니다. 후보가 충분하지 않고 `has_more`가 참이면 `limit`(최대 50)을 높여 다시 조회합니다. 성공한 도구 호출의 JSON은 `structuredContent`에서 읽습니다. 오류 메시지는 `content`에 들어갑니다.

실행용 시맨틱 모델 전체는 MCP 도구로 제공하지 않습니다. LLM이 모델 전체를 불러오지 않게 하기 위해서입니다. 파일 없이 SQL을 생성하는 클라이언트(`t2s`)는 HTTP 앱의 `GET /snapshot`(기본 모델) 또는 `GET /snapshot/{model}`을 `/mcp`와 같은 Bearer 토큰으로 호출합니다. 응답은 `tenant`, `model`, `version_id`, `definition`이 든 JSON이며 1 MiB까지 허용합니다. 서버는 버전별로 만든 정의를 메모리에 최대 8개 보관하므로, 같은 활성 버전을 다시 요청하면 활성 버전 확인 조회 한 번으로 답합니다. 클라이언트가 이전 응답의 ETag를 `If-None-Match`로 보내면 인증과 활성 모델 확인을 거친 뒤 내용이 같을 때 HTTP 304를 반환합니다. 오류는 `{"error": {"code", "message"}}`와 상태 코드(모델 없음 404, Neo4j 접속 실패 503, 그 밖 500)로 반환합니다. stdio 실행에는 이 경로가 없습니다.

모든 정의 조회는 `ActiveVersion.commit`과 `Model.version`이 일치하는 현재 버전으로 한정됩니다. 응답의 `version_id`는 이 활성 커밋 값입니다. 물리 컬럼은 `source_id`, `catalog`, `schema`, `table`, `column`으로 식별합니다. `row_set_candidate`는 `DERIVED_FROM`의 `roles`(예전 그래프는 `role`)에 `join`이 든 영향 후보입니다. 값과 조인 키를 함께 맡는 필드는 값 영향(`impact: value`)이면서 `row_set_candidate: true`입니다. SQL 소스의 물리 테이블 목록은 필드 계보에서 찾은 범위만 반환하며 `tables_basis=field_lineage`로 표시합니다.

상세한 도구 계약과 구현 경계는 [MCP 설계 문서](docs/graphdb-mcp-design.md)에 있습니다. `graphdb/load_original.py`는 최초 GraphDB 설계를 재현하는 별도 코드이며 MCP 서버에서 사용하지 않습니다.

## 확인

```sh
uv run python -m unittest discover -s tests -v
```

GitHub Actions의 이미지 게시는 `uv sync --frozen`과 이 단위 테스트가 통과해야 진행합니다. 이미지는 `uv.lock`의 버전으로만 설치합니다(`uv sync --locked`).

로컬 그래프 통합 테스트는 `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`를 설정한 뒤 `GRAPH_INTEGRATION_TEST=1`을 추가하면 실행됩니다.

## 변경 사항

### 0.3.0 (깨지는 변경)

- `graph_get_model_snapshot` 도구를 지웠습니다. 실행용 모델은 같은 Bearer 토큰으로 `GET /snapshot` 또는 `GET /snapshot/{model}`에서 받습니다. text2sql-plugin 0.5.1 이하는 이 서버에서 `graph_unavailable`로 실패하므로 플러그인을 0.6.0 이상으로 올립니다. 배포는 이 서버를 먼저, 플러그인을 다음에 합니다.
- `graph_find_join_path`를 추가했습니다. 승인된 경로가 없으면 승인 전 연결로 가는 경로를 `unapproved_paths`로 함께 줍니다.
- `GET /snapshot`도 `/mcp`와 같은 토큰·Host 허용 목록을 거칩니다.
- 계보 조회가 `roles`를 읽습니다. 이 값은 semantic-definitions의 게시 코드로 다시 게시해야 그래프에 생깁니다.
- 검색은 참조명·이름·동의어가 정확히 같은 항목을 먼저, 그다음 전문 검색 인덱스의 BM25 관련도 순으로 정렬하고 항목마다 `score`를 줍니다. 인덱스(`definition_text`)는 semantic-definitions가 게시할 때 만들고, 없으면 예전 부분 일치 검색을 씁니다. 데이터셋 소스 판별을 t2s와 같은 규칙으로 바꿨습니다. Neo4j 오류를 코드로 나눠 `SCHEMA_INCOMPATIBLE`과 `DB_UNAVAILABLE`로 돌려줍니다.
- 기본 검색 결과를 1건으로 줄이고 성공 응답의 텍스트 사본을 제거했습니다.

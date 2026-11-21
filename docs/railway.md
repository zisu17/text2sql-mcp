# Railway에서 Graph MCP 운영

## 구성

Railway의 Docker 서비스가 `text2sql_mcp.http_app:app`을 Streamable HTTP로 제공한다. 클라이언트는 `https://<공개 도메인>/mcp`에 Bearer 토큰을 보내고 서버는 `neo4j+s://`로 AuraDB를 조회한다. `text2sql_mcp.server`의 로컬 stdio 실행은 계속 지원한다. 이 서비스는 그래프를 적재하지 않는다.

`GET /healthz`는 인증 없이 `ok`를 반환하는 **프로세스 상태 확인**이다. AuraDB 접속이나 그래프 적재 상태까지 확인하지 않는다. `/mcp`의 모든 HTTP 요청에는 Bearer 토큰이 필요하다. Host 허용 목록에는 `MCP_PUBLIC_HOST`를 사용하고 이 값이 없으면 Railway의 `RAILWAY_PUBLIC_DOMAIN`을 사용한다. 브라우저 Origin은 허용하지 않는다. 이 인증은 스터디용 공유 토큰이며 사용자별 권한이나 MCP OAuth를 제공하지 않는다.

## Railway 설정

1. `main`에 코드를 푸시하면 GitHub Actions가 루트 `Dockerfile`로 `ghcr.io/zisu17/text2sql-mcp:latest` 이미지를 게시한다. 최초 게시 후 패키지 공개 범위가 `Public`인지 확인한다.
2. Railway에서 프로젝트를 만들고 서비스 소스에 공개 이미지를 지정한다.
3. 서비스의 Networking에서 공개 도메인을 생성한다. Docker 앱은 `0.0.0.0`의 Railway `PORT`로 듣는다.
4. 아래 변수를 서비스에 설정한다. 저장소나 Docker 이미지에 비밀번호를 넣지 않는다.
5. Healthcheck 경로를 `/healthz`로 지정한 뒤 배포한다.

| 변수 | 값 |
|---|---|
| `NEO4J_URI` | AuraDB의 `neo4j+s://...databases.neo4j.io` 주소 |
| `NEO4J_USER` | AuraDB 사용자 |
| `NEO4J_PASSWORD` | AuraDB 비밀번호 |
| `NEO4J_DATABASE` | AuraDB 인스턴스의 데이터베이스명 |
| `GRAPH_TENANT` | 현재 Olist 그래프는 `olist` |
| `GRAPH_DEFAULT_MODEL` | 현재 Olist 그래프는 `olist_sales` |
| `MCP_API_TOKEN` | Neo4j 비밀번호와 다른 32자 이상의 무작위 ASCII 토큰 |
| `MCP_PUBLIC_HOST` | 선택. Railway 도메인 자동 감지가 안 될 때 호스트 이름만 지정 |

`MCP_PUBLIC_HOST`에는 `https://`나 `/mcp`를 붙이지 않는다. 첫 Railway 배포에서 공개 도메인이 아직 없으면 인증과 Origin 차단을 유지한 채 Railway 프록시의 Host 검증을 사용한다. 도메인이 생성되면 `MCP_PUBLIC_HOST`를 설정해 MCP 서버의 Host 허용 목록을 고정한다.

토큰 생성 예시:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 확인

```sh
curl -i https://<공개 도메인>/healthz
curl -i https://<공개 도메인>/mcp
```

첫 요청은 `200 OK`와 `ok`를 반환하고 두 번째 요청은 `401 Unauthorized`를 반환해야 한다. 클라이언트의 MCP 서버 설정에서 `Authorization: Bearer <MCP_API_TOKEN 값>` 헤더를 지정한다. `graph_list_models()`가 `olist_sales`를 반환하면 AuraDB까지 연결된 것이다. 빈 결과라면 AuraDB에서 활성 모델을 확인한다.

```cypher
MATCH (a:ActiveVersion), (m:Model)
WHERE a.tenant = m.tenant AND a.model = m.model AND a.commit = m.version
RETURN a.tenant, a.model, a.commit;
```

정의 변경은 `semantic-definitions`가 AuraDB에 게시하면 다음 MCP 조회부터 반영된다. MCP 코드를 바꾼 경우에만 Railway 서비스를 다시 배포한다.

현재 운영 서비스는 `https://text2sql-graph-mcp-production.up.railway.app/mcp`에 있다. Railway 프로젝트는 [text2sql-mcp](https://railway.com/project/4c122e3f-d265-4394-b891-30d89932c428)이다. `MCP_API_TOKEN`은 Railway 서비스 Variables에서 확인한다.

코드를 바꾸면 GitHub Actions가 새 `latest` 이미지를 게시한다. Railway Source의 이미지 자동 업데이트는 `Update to the latest tag`와 `As soon as ready`로 설정했다. Railway가 새 SHA를 감지하면 운영 서비스에 배포한다. 감지는 레지스트리 확인 주기에 따라 몇 시간 늦어질 수 있다.

## 참고

- [Railway Docker 서비스](https://docs.railway.com/services)
- [Railway 자동 환경변수](https://docs.railway.com/variables/reference)
- [Railway 이미지 자동 업데이트](https://docs.railway.com/deployments/image-auto-updates)
- [MCP Python SDK의 Host 허용 목록](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/deploy.md)

# SEC-OPS Gate 1B.1-CI-R1

## Causas confirmadas

- `postgres_ci.py` executava `command.upgrade()` de dentro de `asyncio.run(main(...))`.
  O Alembic carrega `alembic/env.py`, que chama `asyncio.run(run_migrations_online())`;
  o loop já ativo impedia essa execução e produzia o aviso de coroutine não aguardada.
- O frontend usa npm, mas não tinha `package-lock.json`, embora o workflow executasse
  `npm ci`, que exige um lockfile consistente.
- Depois de instalar as dependências, o build também expôs os tipos ausentes de React
  e React DOM e a declaração de imports CSS do Vite ausente no `tsconfig`.
- `ruff check app tests` reportava 246 problemas: imports/nomes não usados e ordenação,
  regras de estilo e comprimentos de linha, além do padrão `Depends(...)` do FastAPI
  apontado por `B008`.
- Na primeira execução após a correção do loop, as migrations falharam porque os enums
  PostgreSQL eram criados explicitamente com `checkfirst=True` e novamente pelo evento
  automático do `sa.Enum` na criação/alteração das tabelas.

## Correções

- A orquestração PostgreSQL agora é síncrona. As verificações assíncronas rodam em
  loops próprios, e o comando síncrono do Alembic roda entre esses loops. Permanecem
  obrigatórios `TEST_DATABASE_URL`, a validação fail-closed de identidade antes das
  migrations, a repetição do preflight e a validação do head e das colunas depois delas.
  Erros de conexão continuam sem imprimir URL ou credenciais.
- O teste de regressão cobre a ordem identidade → upgrade fora de loop → identidade →
  schema, substituindo conexões e Alembic por mocks; não exige PostgreSQL.
- Os enums PostgreSQL das migrations 0002, 0004 e 0005 mantêm a criação explícita e
  desativam a criação automática da coluna, evitando a tentativa duplicada de criar o
  mesmo tipo.
- `frontend/package-lock.json` foi gerado a partir do `package.json`; o workflow mantém
  `npm ci`. Os tipos de React/React DOM foram declarados em `devDependencies`, e
  `vite/client` foi habilitado no `tsconfig` para os imports CSS.
- Foram aplicados os fixes seguros do Ruff e a formatação. As ocorrências remanescentes
  foram revisadas: as chamadas `Depends` e `require_permission` são dependências de rota
  FastAPI intencionais e têm supressões `B008` pontuais nas linhas correspondentes;
  os enums `str, Enum` passaram para `enum.StrEnum`; os comprimentos de linha foram
  corrigidos. Nenhuma regra global foi desativada e nenhum fix inseguro foi usado.

## Validação local

Resultados desta revisão:

- `ruff check app tests`: passou.
- `mypy` nos módulos de API, modelos e serviços alterados: passou; 9 source files,
  sem problemas.
- `pytest tests/test_postgres_ci_config.py tests/test_discovery_enrichment.py
  tests/test_security.py`: 20 passaram.
- `python -m compileall -q app scripts tests`: passou.
- `npm ci` seguido de `npm run build`: passou; TypeScript compilou e o Vite gerou
  o bundle de produção.
- `git diff --check`: passou.
- A tentativa da suíte unitária completa não terminou neste ambiente; os resultados
  acima cobrem os testes diretamente relevantes para as mudanças.

PostgreSQL e Docker não foram executados localmente. Na execução remota 44741176309, o
serviço PostgreSQL iniciou, o preflight passou, porém migration 0002 falhou com
`type "prospecting_run_status" already exists` e os testes de integração foram pulados.
Backend e frontend passaram nessa execução. A validação da correção de enums e os testes
de integração aguardam uma nova execução no GitHub Actions.

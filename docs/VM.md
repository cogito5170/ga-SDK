# VM 에서 GA Engine 돌리기 (`ga vm`)

## 무엇이 어디서 도나

- **VM**: GA Engine 만. 콘솔(`ga console`, 127.0.0.1:8765)과 허브(`ga hub tick --shadow`, 1분마다, 지원되는 버전부터).
- **Mac**: Token 개발 스택, agy 브리지. 이 둘은 VM 과 git 우편함(mailbox)으로만 이야기합니다. (`--full` 이면 이것도 VM 으로: 아래 "Mac 을 끄고 전부 VM 으로")
- 상태는 전부 git 에 있습니다. VM 은 언제든 버려도 되고, 명령 한 줄로 다시 만듭니다.
- 설치기는 `sudo` 를 쓰지 않고, 비밀번호·토큰·ssh 키 파일을 읽지도 쓰지도 않습니다.

## 처음 설치

먼저 점검만 합니다. 결과는 JSON 한 줄이고, 준비가 안 되었으면 0 이 아닌 값으로 끝납니다.

```
cd ~
git clone -b claude/gracious-meitner-vp49xe https://github.com/cogito5170/ga-sdk.git ~/ga-sdk
python3 -m venv ~/ga-boot
~/ga-boot/bin/pip install --no-cache-dir -e ~/ga-sdk
~/ga-boot/bin/python -m ga vm check
```

점검이 통과하면 설치합니다. 먼저 `--dry-run` 으로 무엇을 할지만 볼 수 있습니다.

```
~/ga-boot/bin/python -m ga vm install --dry-run
~/ga-boot/bin/python -m ga vm install
```

설치가 하는 일: `~/baseline`, `~/ga-sdk` 를 `claude/gracious-meitner-vp49xe` 로 받거나 앞으로 감기(fast-forward)만 하고, `~/ga-venv` 를 만들어 ga-sdk 를 설치하고, `~/.ga/console.json` 과 `~/.config/systemd/user/` 의 서비스 파일을 씁니다. 한 번 더 돌려도 바뀐 것이 없으면 아무 파일도 건드리지 않습니다. 내 변경이 남아 있는 체크아웃이 있으면 설치는 멈춥니다(아무것도 지우거나 되돌리지 않습니다).

상태 보기:

```
~/ga-venv/bin/python -m ga vm status
```

## Mac 에서 콘솔 열기

콘솔은 VM 안에서 127.0.0.1 에만 열립니다. Mac 에서 터널을 열고:

```
ssh -N -L 8765:127.0.0.1:8765 <vm 이름>
```

주소(한 번만 보이는 토큰 포함)는 VM 의 로그에 있습니다:

```
journalctl --user -u ga-console -n 20 --no-pager
```

그 주소를 Mac 브라우저에 붙여 넣으면 됩니다.

## 사용자가 직접 하는 일

1. **GitHub 쓰기 권한**: deploy key 또는 `gh auth` 를 직접 설정합니다. 설치기는 이 파일들을 열지 않습니다.
2. **로그아웃해도 서비스가 살아 있게(lingering)**: 아래 한 줄을 직접 실행합니다.

```
sudo loginctl enable-linger $USER
```

3. **/tmp 의 오래된 pip 폴더 비우기**: `ga vm check` 가 `pip-target-*`, `pip-unpack-*`, `pip-build-*` 중 큰 것을 크기와 함께 보여 줍니다(지우지는 않습니다). 지금 돌고 있는 pip 가 없는지 먼저 확인합니다.

```
pgrep -a pip
```

아무것도 안 나오면 목록에 나온 폴더를 직접 지웁니다.

4. 비밀 파일(토큰, agy 로그인)은 직접 둡니다. agy 가 없어도 괜찮습니다. 브리지는 Mac 에 있습니다.

## 디스크 보호

두 서비스는 시작 전에 `ga vm check --disk-only` 를 돌립니다. 여유가 3 GB 보다 적으면 서비스가 시작하지 않고 로그에 이유가 한 줄 남습니다(상태가 깨지는 것보다 낫습니다). 공간을 확보한 뒤 `systemctl --user restart ga-console` 합니다.

## 허브(shadow)

설치된 ga 에 `ga hub tick --shadow` 가 없으면 허브 서비스는 설치만 되고 꺼져 있습니다(이유가 출력됩니다). ga 를 갱신한 뒤:

```
~/ga-venv/bin/python -m ga vm enable-hub
```

허브 유닛은 `ga hub tick --shadow --config ~/.ga/hub.json --ga-dir ~/.ga` 로 돕니다. `ga vm install --full` 이
`~/.ga/hub.json` 을 없을 때만 씁니다(절대 경로, 기존 파일은 건드리지 않음). 파일이 없으면 유닛은 한 줄 이유를 남기고
실패합니다. shadow 결정마다 notify/1(kind shadow) 한 통이 mailbox 의 `baseline-shadow` 로 가고(한 번만), baseline 은
VM 에 로그인하지 않고 `ga hub shadow-compare FILE --mailbox ~/baseline --name baseline-shadow` 로 비교합니다.

## VM 이 회수되어 새로 만들 때

새 VM 에서 위 "처음 설치"를 그대로 다시 하면 됩니다. 상태는 git 에 있으므로 잃는 것이 없습니다. 클라우드의 매시간 점검은 안전망으로 그대로 둡니다.

## 지우기

```
~/ga-venv/bin/python -m ga vm uninstall
```

서비스를 멈추고 서비스 파일과 `~/.ga/console.json` 을 지웁니다. `~/baseline`, `~/ga-sdk`, `~/ga-venv` 는 그대로 둡니다.

## Mac 을 끄고 전부 VM 으로 (`ga vm install --full`)

`--full` 은 위 설치에 더해 Mac 이 하던 일을 VM 으로 옮깁니다: `~/token`(Token 스택: token-api, token-worker, token-web), agy 플러그인 에이전트(minimal, ga-plan, ga-act, ga-ask), agy 브리지(`ga-bridge.service`). 모든 서비스는 127.0.0.1 에만 열립니다. 포트를 여는 일은 없고, 다른 PC 에서는 SSH 터널로만 들어옵니다.

```
~/ga-venv/bin/python -m ga vm install --full --dry-run
~/ga-venv/bin/python -m ga vm install --full
```

하는 일: `~/token` 을 받고, `~/token/.venv` 에 backend 를 설치하고, `~/token/frontend` 에서 `npm ci` 를 합니다(임시 폴더와 npm 캐시는 따로 만들고 끝나면 지움; `npm ci` 앞뒤로 디스크 검사, 모자라면 멈추고 새로 켜는 것은 없음). `~/.ga/console.json` 에 Token 서비스 셋과 브리지 항목을 VM 경로로 씁니다. `~/.ga-ask/ask.json` 이 없을 때만 `{"ask_agent": "ga-ask"}` 로 만듭니다(GA44 의 묻기 기본 에이전트)(있으면 건드리지 않음). agy 가 있으면 에이전트 셋을 설치하고, 없으면 실행할 명령만 보여 줍니다.

설치기가 하지 않는 일(명령만 출력합니다 — sudo 도, 비밀 값도 없음):

- **PostgreSQL 16** 과 `gaconsole` DB (`sudo apt-get install -y postgresql` 등).
- **`~/token/.env`**: `python3 scripts/dev_env.py --db-host localhost` 를 직접 실행합니다. 값은 이 VM 에서 만들어지고 출력되지 않습니다. 필요한 이름만 `.env.example` 에서 읽어 보여 줍니다.
- **`~/agy-bridge.json`**: Mac 의 것을 복사하고 경로를 VM 의 홈으로 바꿉니다.
- agy 설치와 로그인.

빠진 것이 있으면 해당 서비스는 console.json 에 `disabled` 로 남고(콘솔이 시작하지 않음), 준비한 뒤 `install --full` 을 한 번 더 돌리면 켜집니다. 브리지는 콘솔이 아니라 `ga-bridge.service` 가 돌립니다.

## 다른 PC 에서 콘솔과 묻기 열기

1. **SSH 키 로그인**: 그 PC 의 공개키를 VM 의 `~/.ssh/authorized_keys` 에 직접 넣습니다(설치기는 키 파일을 열지 않습니다).
2. VM 에서 주소와 터널 명령을 봅니다:

```
~/ga-venv/bin/python -m ga vm url --host <vm 공인 IP>
```

3. 그 PC 에서 터널을 엽니다(같은 포트라서 콘솔의 Host 검사가 통과합니다):

```
ssh -N -L 8765:127.0.0.1:8765 <user>@<vm 공인 IP>
```

4. 출력된 주소를 브라우저에 붙여 넣습니다. 묻기는 같은 주소 끝의 `#/ask` 입니다.

## Mac 끄기 체크리스트

1. **Mac 의 브리지를 끕니다.** 브리지 둘이 같은 지시를 두 번 답합니다.
2. VM 에서 `~/ga-venv/bin/python -m ga vm bridge-adopt` — 지금 `to/AGY` 에 있는 메시지를 모두 읽음으로 표시합니다(몇 개인지 출력). 새 브리지가 옛 지시를 다시 돌리지 않게 합니다.
3. `~/ga-venv/bin/python -m ga vm enable-bridge --yes` — `--yes` 없이는 켜지 않습니다. agy, adopt 표시, `~/agy-bridge.json` 중 하나라도 없으면 서비스는 시작 전에 이유 한 줄을 로그에 남기고 멈춥니다.
4. 시험 지시 하나를 `to/AGY` 로 보내고, VM 의 브리지가 답하는지 `journalctl --user -u ga-bridge -n 20` 으로 봅니다.
5. 그다음 Mac 을 끕니다.

## 되돌리기

```
systemctl --user disable --now ga-bridge.service
```

그다음 Mac 에서 `~/ga-venv/bin/python -m ga vm bridge-adopt` 를 한 번 돌리고(그동안 VM 이 답한 지시를 Mac 의 읽음 기록에도 표시) Mac 의 브리지를 다시 켭니다. 전부 지우려면 `ga vm uninstall`(서비스와 설정만 지우고 `~/token` 과 체크아웃은 남김).

## 스스로 최신 유지 (`ga vm update`)

`ga vm install --full` 은 `ga-update.timer` 도 켭니다(부팅 2분 뒤, 이후 30분마다). `ga vm update [--dry-run]` 은 디스크 가드 후 `~/ga-sdk`, `~/baseline`, `~/token` 을 `origin/<통합 브랜치>` 로 fast-forward 만 합니다(로컬 변경이 있거나 ff 가 안 되면 그 한 줄만 남기고 건너뜀 — reset/force 없음). ga-sdk 의 HEAD 나 pyproject 가 바뀐 때만 `pip install -e`, ga-sdk 가 바뀐 때만 켜져 있는 ga-console·ga-bridge 를 재시작합니다(꺼진 유닛은 건드리지 않음). 한 번 실행에 한 줄이 `~/.ga/update.jsonl` 에 쌓이고 `ga vm status` 가 마지막 줄을 보여줍니다. ga 버전이 새로워지면 `baseline-ops` 로 notify/1(ack) 한 통을 보냅니다(실패하면 다음 실행에서 다시).

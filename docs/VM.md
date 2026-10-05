# VM 에서 GA Engine 돌리기 (`ga vm`)

## 무엇이 어디서 도나

- **VM**: GA Engine 만. 콘솔(`ga console`, 127.0.0.1:8765)과 허브(`ga hub tick --shadow`, 1분마다, 지원되는 버전부터).
- **Mac**: Token 개발 스택, agy 브리지. 이 둘은 VM 과 git 우편함(mailbox)으로만 이야기합니다.
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

## VM 이 회수되어 새로 만들 때

새 VM 에서 위 "처음 설치"를 그대로 다시 하면 됩니다. 상태는 git 에 있으므로 잃는 것이 없습니다. 클라우드의 매시간 점검은 안전망으로 그대로 둡니다.

## 지우기

```
~/ga-venv/bin/python -m ga vm uninstall
```

서비스를 멈추고 서비스 파일과 `~/.ga/console.json` 을 지웁니다. `~/baseline`, `~/ga-sdk`, `~/ga-venv` 는 그대로 둡니다.

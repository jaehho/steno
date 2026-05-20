# Maintainer: Jaeho Cho <jaewon0907@gmail.com>
#
# Local-build PKGBUILD. From the project root:
#   paru -Bi                 # build + install, resolves AUR deps (recommended)
#   makepkg -si              # works only if python-anthropic is already installed
#                            # (it's AUR-only, so pure makepkg can't fetch it)
#   sudo pacman -R meeting-copilot   # uninstall
#
# Bump `pkgver` to match pyproject.toml on each release; bump `pkgrel`
# when you change the PKGBUILD itself without bumping the version.

pkgname=meeting-copilot
pkgver=0.2.0
pkgrel=1
pkgdesc="Live meeting copilot: dual audio capture, Deepgram transcription, Claude advisor TUI"
arch=('any')
url=""
license=('custom')
depends=(
  'python'
  'python-anthropic'
  'python-textual'
  'python-websockets'
  'python-dotenv'
  'python-sounddevice'
  'python-soundfile'
  'python-numpy'
  'libpulse'
  'portaudio'
)
makedepends=(
  'python-build'
  'python-installer'
  'python-setuptools'
)
# Local build: no remote source. We copy the project into $srcdir in prepare().
source=()
sha256sums=()

prepare() {
  rm -rf "$srcdir/$pkgname"
  mkdir -p "$srcdir/$pkgname"
  cp -r "$startdir"/{src,pyproject.toml} "$srcdir/$pkgname/"
}

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
}

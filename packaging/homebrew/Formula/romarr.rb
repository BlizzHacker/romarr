class Romarr < Formula
  include Language::Python::Virtualenv

  desc "*arr for games: request, find, verify and file games into your library"
  homepage "https://romarr.app"
  url "https://github.com/BlizzHacker/romarr/archive/refs/tags/v1.10.0.tar.gz"
  sha256 "ff329ec6e6eb99176ae54e753b850ae225c4061e0f9c688517d12a5be3106cb6"
  license "MIT"
  head "https://github.com/BlizzHacker/romarr.git", branch: "main"

  depends_on "certifi"
  depends_on "python@3.14"

  on_linux do
    depends_on "libarchive"
  end

  resource "charset-normalizer" do
    url "https://files.pythonhosted.org/packages/33/1c/f41d4e74c28ab327ff3acd36053f7ea506c55872d7a90b0fa71aa3ab0c89/charset_normalizer-3.5.2.tar.gz"
    sha256 "39de2a259fc954455c57274dc94c79d5842774e1247a016aff30bc0efed0f4ef"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/f5/08/8eea9d4b8302028f3abb2c0813953f7aec26d33b7a8960ed760e65ff29fa/idna-3.20.tar.gz"
    sha256 "a7db850025b95ded1eae8a46181a1a6c56c92c96f0e2b005d9ff8dc0210cab44"
  end

  resource "requests" do
    url "https://files.pythonhosted.org/packages/ac/c3/e2a2b89f2d3e2179abd6d00ebd70bff6273f37fb3e0cc209f48b39d00cbf/requests-2.34.2.tar.gz"
    sha256 "f288924cae4e29463698d6d60bc6a4da69c89185ad1e0bcc4104f584e960b9ed"
  end

  resource "urllib3" do
    url "https://files.pythonhosted.org/packages/e3/05/b17359e1cefb4f909b5e40b1b90a496d987258916dbbf88e842c729f510e/urllib3-2.8.0.tar.gz"
    sha256 "63bf2ead4c879426ebf22ef2a781eeb4aa3b4ae798a0435506f8687fd5bb9b63"
  end

  def install
    venv = virtualenv_create(libexec, "python3.14")
    venv.pip_install resources
    libexec.install "romarr"
    (libexec/"romarr/_version.py").write "__version__ = \"#{version}\"\n"

    # Loopback by default: a fresh ROMarr is unclaimed until the first visitor
    # sets its password, so it must not appear on the LAN before that happens.
    # Settings are per user, so the service and a shell run share one state.
    extra_path = OS.linux? ? "#{formula_opt_bin("libarchive")}:" : ""
    (bin/"romarr").write <<~SH
      #!/bin/bash
      export PYTHONPATH="#{libexec}${PYTHONPATH:+:$PYTHONPATH}"
      export PATH="#{extra_path}$PATH"
      export ROMARR_HOST="${ROMARR_HOST:-127.0.0.1}"
      state="${XDG_DATA_HOME:-$HOME/.local/share}/romarr"
      [ "$(uname -s)" = "Darwin" ] && state="$HOME/Library/Application Support/ROMarr"
      export ROMARR_DATA="${ROMARR_DATA:-$state/romarr.json}"
      export ROM_HUB_HOME="${ROM_HUB_HOME:-$state/rom-hub}"
      # Never an inherited LIBRARY_PATH: on a Mac that is usually the linker's
      # search path (/opt/homebrew/lib), and ROMarr would file games into it.
      unset ROMM_LIBRARY
      export LIBRARY_PATH="${ROMARR_LIBRARY:-$HOME/Games/ROMs}"
      mkdir -p "$(dirname "$ROMARR_DATA")"
      exec "#{libexec}/bin/python" -m romarr "$@"
    SH
  end

  def caveats
    <<~EOS
      ROMarr listens on http://127.0.0.1:6868 and the first visit sets its password.
      To reach it from other machines, set ROMARR_HOST=0.0.0.0 after claiming it.
      Settings live in ~/Library/Application Support/ROMarr (macOS) or
      ~/.local/share/romarr (Linux); games are filed into ~/Games/ROMs unless
      ROMARR_LIBRARY is set on first start (later, change it in Settings).
    EOS
  end

  service do
    run opt_bin/"romarr"
    keep_alive true
    log_path var/"log/romarr.log"
    error_log_path var/"log/romarr.log"
  end

  test do
    port = free_port
    ENV["ROMARR_PORT"] = port.to_s
    ENV["ROMARR_DATA"] = (testpath/"romarr.json").to_s
    ENV["ROMARR_LIBRARY"] = (testpath/"roms").to_s
    pid = spawn bin/"romarr"
    begin
      health = ""
      30.times do
        sleep 1
        health = shell_output("curl -s http://127.0.0.1:#{port}/api/health")
        break if health.include?("ok")
      rescue Minitest::Assertion
        next
      end
      assert_match '"ok": true', health
      assert_path_exists testpath/"romarr.json"
    ensure
      Process.kill "TERM", pid
      Process.wait pid
    end
  end
end

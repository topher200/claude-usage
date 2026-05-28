class ClaudeUsage < Formula
  desc "Token, cost, and session dashboard for Claude Code usage"
  homepage "https://github.com/phuryn/claude-usage"
  # URL and sha256 pinned to the current main commit (v1.1.0 release).
  # The project intentionally doesn't tag releases, so this formula needs
  # to be bumped each release. See CHANGELOG.md for what each commit ships.
  url "https://github.com/phuryn/claude-usage/archive/256b3e839acbaa2b850345854d77fe8dfd15e44e.tar.gz"
  version "1.1.0"
  sha256 "1ee3c31268100d63ca137af253ccfaaba1781e4043d3f5d84e74bcf4ec9c5133"
  license "MIT"
  head "https://github.com/phuryn/claude-usage.git", branch: "main"

  depends_on "python@3.13"

  def install
    libexec.install "cli.py", "scanner.py", "dashboard.py"

    (bin/"claude-usage").write <<~EOS
      #!/bin/bash
      exec "#{Formula["python@3.13"].opt_bin}/python3" "#{libexec}/cli.py" "$@"
    EOS
    chmod 0755, bin/"claude-usage"
  end

  test do
    # 1. No-args invocation prints the usage banner — exercises the shim.
    output = shell_output("#{bin}/claude-usage")
    assert_match "Claude Code Usage Dashboard", output
    assert_match "scan", output
    assert_match "dashboard", output

    # 2. `scan` against an empty projects dir exercises the real code path
    #    end-to-end (sqlite open, glob walk, summary print) without touching
    #    the user's real ~/.claude/usage.db. Homebrew's test sandbox provides
    #    testpath, so this stays isolated.
    (testpath/"projects").mkpath
    scan_output = shell_output("#{bin}/claude-usage scan --projects-dir #{testpath}/projects")
    assert_match "Scan complete", scan_output
  end
end

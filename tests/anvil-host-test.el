;;; anvil-host-test.el --- Tests for anvil-host -*- lexical-binding: t; -*-

;;; Commentary:

;; Regression / correctness tests for `anvil-shell' and related
;; `anvil-host' helpers.  Focused on behaviours that are easy to get
;; wrong at the `make-process' boundary — most notably that detached
;; descendants spawned from inside the shell must survive
;; the wrapper shell exiting (issue #10).

;;; Code:

(require 'ert)
(require 'cl-lib)
(require 'anvil-host)

;;;; --- issue #10 guard -----------------------------------------------------

(ert-deftest anvil-host-test-shell-preserves-disowned-children ()
  "Regression guard for issue #10.

A detached descendant spawned inside the shell command must survive
`anvil-shell' returning.  `anvil-host--run' gives the shell pipe
streams and closes its process-input pipe immediately because the
API has no stdin channel.  On Linux, `setsid --fork' creates the new
session needed for a descendant to outlive the wrapper shell.  This
is the same mechanism clipboard helpers such as `wl-copy' and
`xclip' use for their owner daemons.

The test schedules a short-delayed background `touch' via
`setsid --fork' and asserts the marker file appears after the shell
has already returned.  If `anvil-host--run' ever regresses to a mode
that reaps the whole subprocess tree on exit, the setsid grandchild
dies before it can touch the file."
  (skip-unless (eq system-type 'gnu/linux))
  (skip-unless (executable-find "setsid"))
  (let* ((marker (make-temp-file "anvil-host-test-detach-"))
         (cmd (format "setsid --fork sh -c 'sleep 0.5; touch %s'"
                      (shell-quote-argument marker))))
    ;; make-temp-file creates the file; delete so the test can
    ;; observe a real re-creation by the detached child.
    (delete-file marker)
    (unwind-protect
        (progn
          (let ((res (anvil-shell cmd '(:timeout 5))))
            (should (eql 0 (plist-get res :exit))))
          (should-not (file-exists-p marker))  ; background not done yet
          (let ((deadline (+ (float-time) 3.0)))
            (while (and (not (file-exists-p marker))
                        (< (float-time) deadline))
              (sleep-for 0.05)))
          (should (file-exists-p marker)))
      (ignore-errors (delete-file marker)))))

;;;; --- stdout / stderr hygiene --------------------------------------------

(ert-deftest anvil-host-test-shell-does-not-leak-sentinel-status ()
  "`Process anvil-host-shell finished' must not appear in captured
:stdout / :stderr.  Emacs's default process sentinel writes that
line on exit; the wrapper silences it with `:sentinel #'ignore'
(and the same for the `:stderr' pipe's own sentinel)."
  (skip-unless (memq system-type '(gnu/linux darwin)))
  (let ((res (anvil-shell "echo hello; echo world >&2" '(:timeout 3))))
    (should (eql 0 (plist-get res :exit)))
    (let ((out (plist-get res :stdout))
          (err (plist-get res :stderr)))
      (should-not (string-match-p "Process anvil-host-shell" out))
      (should-not (string-match-p "Process anvil-host-shell" err))
      (should (string-match-p "hello" out))
      (should (string-match-p "world" err)))))

;;;; --- basic exit / output semantics --------------------------------------

(ert-deftest anvil-host-test-shell-stdin-is-eof ()
  "Shell commands that read stdin must observe immediate EOF."
  (skip-unless (memq system-type '(gnu/linux darwin windows-nt)))
  (let* ((command
          (if (eq system-type 'windows-nt)
              "more >NUL & echo stdin-eof"
            "cat >/dev/null; printf 'stdin-eof\\n'"))
         (res (anvil-shell command '(:timeout 3))))
    (should (eql 0 (plist-get res :exit)))
    (should (equal "stdin-eof\n" (plist-get res :stdout)))
    (should (equal "" (plist-get res :stderr)))))

(ert-deftest anvil-host-test-child-bindings-are-spawn-local ()
  "Dedicated child bindings apply at spawn without leaking into the root."
  (skip-unless (memq system-type '(gnu/linux darwin)))
  (let* ((root-environment process-environment)
         (root-exec-path exec-path)
         (root-shell shell-file-name)
         (root-switch shell-command-switch)
         (child-environment
          (cons "ANVIL_HOST_CHILD_SCOPE=child"
                (copy-sequence process-environment)))
         (child-exec-path (reverse (copy-sequence exec-path)))
         (child-shell (or (executable-find "sh") shell-file-name))
         (anvil-host-child-process-environment child-environment)
         (anvil-host-child-exec-path child-exec-path)
         (anvil-host-child-shell-file-name child-shell)
         (anvil-host-child-shell-command-switch "-c")
         (original-make-process anvil-host--make-process-primitive)
         observed)
    (cl-letf ((anvil-host--make-process-primitive
               (lambda (&rest args)
                 (setq observed
                       (list process-environment exec-path
                             shell-file-name shell-command-switch))
                 (apply original-make-process args))))
      (let ((result
             (anvil-host--run
              "printf %s \"$ANVIL_HOST_CHILD_SCOPE\""
              'utf-8 temporary-file-directory 3)))
        (should (equal '(0 "child" "") result))))
    (should (equal child-environment (nth 0 observed)))
    (should (equal child-exec-path (nth 1 observed)))
    (should (equal child-shell (nth 2 observed)))
    (should (equal "-c" (nth 3 observed)))
    (should (eq root-environment process-environment))
    (should (eq root-exec-path exec-path))
    (should (eq root-shell shell-file-name))
    (should (eq root-switch shell-command-switch))))

(ert-deftest anvil-host-test-shell-nonzero-exit-reported ()
  "A non-zero shell exit is reported in :exit (not raised)."
  (skip-unless (memq system-type '(gnu/linux darwin)))
  (let ((res (anvil-shell "exit 7" '(:timeout 3))))
    (should (eql 7 (plist-get res :exit)))))

(ert-deftest anvil-host-test-output-limits-count-encoded-bytes ()
  "Presentation limits preserve characters and report omitted bytes."
  (should (equal "c…" (anvil-host--truncate "café" 4)))
  (should (= 4 (string-bytes (anvil-host--truncate "café" 4))))
  (should (equal "café" (anvil-host--truncate "café" 5)))
  (let* ((raw (concat (make-string 64 ?x) "é"))
         (truncated (anvil-host--truncate raw 64)))
    (should (= 64 (string-bytes truncated)))
    (should
     (string-match
      "\n\\.\\.\\.\\[anvil-host: truncated, \\([0-9]+\\) more bytes\\]\\'"
      truncated))
    (let ((prefix (substring truncated 0 (match-beginning 0)))
          (omitted (string-to-number (match-string 1 truncated))))
      (should
       (= omitted (- (string-bytes raw) (string-bytes prefix))))))
  (let ((truncated
         (anvil-shell "printf 'caf\\303\\251'"
                      '(:timeout 3 :max-output 4)))
        (exact
         (anvil-shell "printf 'caf\\303\\251'"
                      '(:timeout 3 :max-output 5))))
    (should (plist-get truncated :truncated))
    (should (equal "c…" (plist-get truncated :stdout)))
    (should (= 4 (string-bytes (plist-get truncated :stdout))))
    (should-not (plist-get exact :truncated))
    (should (equal "café" (plist-get exact :stdout)))))

(ert-deftest anvil-host-test-rejects-invalid-output-limit-before-spawn ()
  "Malformed presentation limits fail before starting a host child."
  (cl-letf (((symbol-function 'anvil-host--run)
             (lambda (&rest _args)
               (ert-fail "invalid output limit reached the host runner"))))
    (dolist (limit '(-1 1.5 "4"))
      (should-error
       (anvil-shell "printf no" (list :max-output limit))
       :type 'error))))

(ert-deftest anvil-host-test-marker-is-inside-every-byte-budget ()
  "Every cap is strict for multibyte and high-byte unibyte source."
  (dolist (raw
           (list
            (concat (make-string 180 ?x) "é漢🙂")
            (apply #'unibyte-string (make-list 180 255))))
    (dolist (cap (number-sequence 0 256))
      (let ((result
             (anvil-host--truncate-with-marker
              raw cap
              (lambda (omitted) (format "…[%d bytes omitted]" omitted)))))
        (should (<= (string-bytes result) cap))))))

;;;; --- §7.2 late stderr capture (stderr-pipe drain) ----------------------

(ert-deftest anvil-host-test-shell-captures-late-stderr ()
  "Stderr emitted just before exit must be fully captured.
Regression for the race between proc and stderr-pipe-proc
lifecycles: the kernel pipe may still hold bytes when proc dies,
and the pipe-proc only reads them once Emacs services it.  The
post-exit drain captures those late bytes."
  (skip-unless (memq system-type '(gnu/linux darwin)))
  (let ((res (anvil-shell
              "for i in 1 2 3 4 5; do echo line$i >&2; done; exit 0"
              '(:timeout 5))))
    (should (eql 0 (plist-get res :exit)))
    (let ((err (plist-get res :stderr)))
      (should (string-match-p "line1" err))
      (should (string-match-p "line5" err)))))

(provide 'anvil-host-test)
;;; anvil-host-test.el ends here

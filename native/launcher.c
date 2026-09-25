/* MicMic.app's main executable, for release builds only.
 *
 * The development build uses a shell script here, which finds a python and execs it.
 * A release build cannot do that, for two separate reasons.
 *
 * TCC. macOS attributes a microphone or speech-recognition prompt to the process that
 * asks, and a process that exec()s a different binary carries THAT binary's identity,
 * not the bundle's. An exec'd python would be asked about as "python3.11", and the
 * NSMicrophoneUsageDescription strings in Info.plist would never be shown. So this
 * links libpython and calls into it in-process: one process, one identity, the
 * bundle's own.
 *
 * Notarization. Every executable in a notarized bundle must be signed with the
 * hardened runtime, and the main executable in particular has to be a Mach-O for that
 * to mean anything. A shell script cannot carry a hardened runtime.
 *
 * Everything is found relative to the executable, so the bundle relocates: a user can
 * keep it anywhere and it still runs.
 */
#include <Python.h>
#include <mach-o/dyld.h>
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static wchar_t *dec(const char *s) { return Py_DecodeLocale(s, NULL); }

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t sz = sizeof exe;
    if (_NSGetExecutablePath(exe, &sz) != 0 || !realpath(exe, real)) {
        fprintf(stderr, "MicMic: cannot locate my own executable\n");
        return 1;
    }
    char macos[PATH_MAX], contents[PATH_MAX];
    snprintf(macos, sizeof macos, "%s", dirname(real));       /* .../Contents/MacOS */
    snprintf(contents, sizeof contents, "%s", dirname(macos));/* .../Contents       */

    char home[PATH_MAX], lib[PATH_MAX], app[PATH_MAX], script[PATH_MAX];
    snprintf(home,   sizeof home,   "%s/Resources/python", contents);
    snprintf(lib,    sizeof lib,    "%s/Resources/lib",    contents);
    snprintf(app,    sizeof app,    "%s/Resources/app",    contents);
    snprintf(script, sizeof script, "%s/listener.py",      app);

    PyStatus st;

    /* PEP 540, and it has to happen in the PRE-config: utf8_mode is read while the
     * interpreter is still deciding its encodings, before PyConfig is consulted.
     * Without it an isolated interpreter takes stdio encoding from a locale that a
     * double-clicked app does not have, settles on ASCII, and the first Hebrew,
     * Arabic or Russian string written to the log raises UnicodeEncodeError. MicMic
     * is a four-language app; ASCII stdio is not survivable. */
    PyPreConfig pre;
    PyPreConfig_InitIsolatedConfig(&pre);
    pre.utf8_mode = 1;
    st = Py_PreInitialize(&pre);
    if (PyStatus_Exception(st)) {
        fprintf(stderr, "MicMic: could not pre-initialise the interpreter\n");
        return 1;
    }

    PyConfig cfg;
    /* Isolated: ignore PYTHONPATH, PYTHONHOME and the user site directory, so a
     * stray environment variable on someone's Mac cannot inject code into a signed
     * app or silently shadow a bundled module. */
    PyConfig_InitIsolatedConfig(&cfg);
    /* Never write __pycache__ inside the bundle: a signed .app is sealed, and 40
     * cache folders written on first run broke the seal ("a sealed resource is
     * missing or invalid"), which macOS can hold against the app on a later launch. */
    cfg.write_bytecode = 0;
    cfg.configure_c_stdio = 1;

    st = PyConfig_SetString(&cfg, &cfg.home, dec(home));
    if (PyStatus_Exception(st)) goto fail;

    /* argv[0] is the script, so sys.argv matches a plain `python3 listener.py`. */
    wchar_t **wargv = calloc((size_t)argc + 1, sizeof *wargv);
    if (!wargv) goto fail;
    wargv[0] = dec(script);
    for (int i = 1; i < argc; i++) wargv[i] = dec(argv[i]);
    st = PyConfig_SetArgv(&cfg, argc, wargv);
    if (PyStatus_Exception(st)) goto fail;

    st = Py_InitializeFromConfig(&cfg);
    if (PyStatus_Exception(st)) goto fail;
    PyConfig_Clear(&cfg);

    /* The dependency tree and the app's own code, in that order. Done here rather
     * than through cfg.module_search_paths so the interpreter is already up and a
     * failure is a normal Python error instead of a silent init abort. */
    PyObject *sys_path = PySys_GetObject("path");
    if (sys_path) {
        PyObject *a = PyUnicode_FromString(lib), *b = PyUnicode_FromString(app);
        if (a) { PyList_Insert(sys_path, 0, a); Py_DECREF(a); }
        if (b) { PyList_Insert(sys_path, 1, b); Py_DECREF(b); }
    }

    FILE *f = fopen(script, "r");
    if (!f) {
        fprintf(stderr, "MicMic: %s is missing from the bundle\n", script);
        return 1;
    }
    int rc = PyRun_SimpleFile(f, script);
    fclose(f);
    if (Py_FinalizeEx() < 0) return 120;
    return rc ? 1 : 0;

fail:
    PyConfig_Clear(&cfg);
    fprintf(stderr, "MicMic: could not start the embedded interpreter\n");
    return 1;
}

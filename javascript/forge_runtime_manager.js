/**
 * Forge Runtime Manager V1
 *
 * V1 intentionally avoids WebSocket overrides, broad MutationObservers,
 * gallery virtualization, and LoRA search workers. Those experiments were
 * too likely to interfere with Gradio-heavy UIs.
 */
(() => {
    "use strict";

    function isShown(element) {
        return element && getComputedStyle(element).display !== "none";
    }

    function markEmergencyInterrupting(tabname) {
        const app = gradioApp();
        const interrupt = app.getElementById(tabname + "_interrupt");
        const interrupting = app.getElementById(tabname + "_interrupting");

        if (!isShown(interrupt) || isShown(interrupting)) return;

        if (typeof showSubmitInterruptingPlaceholder === "function") {
            showSubmitInterruptingPlaceholder(tabname);
        }
    }

    function hasVisibleInterruptControls() {
        const app = gradioApp();
        return ["txt2img", "img2img"].some(function (tabname) {
            return isShown(app.getElementById(tabname + "_interrupt"));
        });
    }

    async function syncEmergencyStopUi() {
        if (!hasVisibleInterruptControls()) return;

        try {
            const response = await fetch("./forge-runtime-manager/state", {
                cache: "no-store",
            });
            if (!response.ok) return;

            const state = await response.json();
            if (!state.emergency_interrupted) return;

            markEmergencyInterrupting("txt2img");
            markEmergencyInterrupting("img2img");
        } catch (_error) {
            // The route is only available after Forge finishes startup.
        }
    }

    onUiLoaded(function () {
        setInterval(syncEmergencyStopUi, 1000);
    });

    console.log("[ForgeRuntimeManager v1.0.3] JS loaded: emergency stop UI sync enabled");
})();

/**
 * Forge Runtime Manager V1
 *
 * V1 intentionally avoids WebSocket overrides, broad MutationObservers,
 * gallery virtualization, and LoRA search workers. Those experiments were
 * too likely to interfere with Gradio-heavy UIs.
 */
(() => {
    "use strict";
    console.log("[ForgeRuntimeManager v1.0.3] JS loaded: no runtime UI hooks enabled");
})();

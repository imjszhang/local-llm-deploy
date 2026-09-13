import { afterEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import DialogShell from "../../src/components/DialogShell.vue";
import SlotOutput from "../../src/components/SlotOutput.vue";

afterEach(() => {
  document.body.innerHTML = "";
  document.body.style.overflow = "";
});

describe("dialog keyboard navigation", () => {
  it("keeps Tab in the dialog, dismisses on Escape, and restores the opening control", async () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    document.body.style.overflow = "auto";
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      { width: 10, height: 10 },
    ] as unknown as DOMRectList);
    const wrapper = mount(DialogShell, {
      props: { labelId: "title" },
      slots: {
        default:
          '<h2 id="title">Details</h2><button id="first">First</button><button disabled>Disabled</button><a id="last" href="#last">Last</a>',
      },
      attachTo: document.body,
    });
    await flushPromises();
    const panel = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const first = document.querySelector<HTMLElement>("#first")!;
    const last = document.querySelector<HTMLElement>("#last")!;
    expect(document.activeElement).toBe(panel);
    expect(document.body.style.overflow).toBe("hidden");
    panel.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(document.activeElement).toBe(first);
    first.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(document.activeElement).toBe(last);
    last.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(document.activeElement).toBe(first);
    first.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
    expect(document.activeElement).toBe(opener);
    expect(document.body.style.overflow).toBe("auto");
  });

  it("keeps focus on an empty dialog when no controls are available", async () => {
    const wrapper = mount(DialogShell, {
      props: { labelId: "empty-title" },
      slots: { default: '<h2 id="empty-title">Empty</h2>' },
      attachTo: document.body,
    });
    await flushPromises();
    const panel = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const event = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    panel.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(panel);
    wrapper.unmount();
  });

  it("still dismisses when async content removes the focused control", async () => {
    const wrapper = mount(DialogShell, {
      props: { labelId: "async-title" },
      slots: {
        default:
          '<h2 id="async-title">Async</h2><button id="removed-control">Output</button>',
      },
      attachTo: document.body,
    });
    await flushPromises();
    const removed = document.querySelector<HTMLElement>("#removed-control")!;
    removed.focus();
    removed.remove();
    expect(document.activeElement).toBe(document.body);
    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Escape",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(wrapper.emitted("close")).toHaveLength(1);
    wrapper.unmount();
  });
});

describe("slot output reading position", () => {
  it("preserves an upward reading position and resumes only on an explicit latest action", async () => {
    const wrapper = mount(SlotOutput, {
      props: { text: "first", label: "Output" },
      attachTo: document.body,
    });
    const viewport = wrapper.get("pre").element;
    let height = 1000;
    Object.defineProperty(viewport, "scrollHeight", {
      get: () => height,
      configurable: true,
    });
    Object.defineProperty(viewport, "clientHeight", {
      value: 200,
      configurable: true,
    });
    viewport.scrollTop = 350;
    await wrapper.get("pre").trigger("scroll");
    await wrapper.setProps({ text: "second\n".repeat(200) });
    await flushPromises();
    expect(viewport.scrollTop).toBe(350);
    expect(wrapper.get("button").text()).toContain("回到最新");
    await wrapper.get("button").trigger("click");
    expect(viewport.scrollTop).toBe(1000);
    height = 1200;
    await wrapper.setProps({ text: "third\n".repeat(250) });
    await flushPromises();
    expect(viewport.scrollTop).toBe(1200);
    expect(wrapper.find("button").exists()).toBe(false);
    wrapper.unmount();
  });

  it("renders hostile output as literal text", () => {
    const text = '<img src=x onerror="window.injected=true">';
    const wrapper = mount(SlotOutput, { props: { text, label: "Output" } });
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.get("pre").text()).toBe(text);
    wrapper.unmount();
  });
});

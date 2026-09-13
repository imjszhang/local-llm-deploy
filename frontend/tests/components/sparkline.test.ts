import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import Sparkline from "../../src/components/Sparkline.vue";

describe("trend time axis", () => {
  it("plots five-second and sixty-second intervals at their real spacing", () => {
    const wrapper = mount(Sparkline, {
      props: {
        values: [20, 25, 30],
        times: [100000, 105000, 165000],
        now: 200000,
        max: 100,
        label: "CPU",
      },
    });
    const x = wrapper
      .get("polyline")
      .attributes("points")!
      .split(" ")
      .map((point) => Number(point.split(",")[0]));
    expect((x[2]! - x[1]!) / (x[1]! - x[0]!)).toBeCloseTo(12, 1);
    wrapper.unmount();
  });

  it("keeps missing readings as gaps and removes samples outside the five-minute window", () => {
    const wrapper = mount(Sparkline, {
      props: {
        values: [1, 20, null, 30],
        times: [1, 150000, 160000, 170000],
        now: 400000,
        max: 100,
        label: "CPU",
      },
    });
    expect(wrapper.find("polyline").exists()).toBe(false);
    expect(wrapper.findAll("circle")).toHaveLength(2);
    wrapper.unmount();
  });
});

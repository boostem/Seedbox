// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Search } from "lucide-react";
import { createRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Field, Input, Select } from "./Input";

afterEach(cleanup);

describe("Input", () => {
  it("types and fires onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    function Box() {
      const [v, setV] = useState("");
      return (
        <Input
          icon={Search}
          aria-label="Search"
          value={v}
          onChange={(e) => {
            setV(e.target.value);
            onChange(e.target.value);
          }}
        />
      );
    }
    render(<Box />);
    await user.type(screen.getByRole("textbox", { name: "Search" }), "deep");
    expect((screen.getByRole("textbox", { name: "Search" }) as HTMLInputElement).value).toBe("deep");
    expect(onChange).toHaveBeenLastCalledWith("deep");
  });

  it("forwards ref, shows its suffix and respects disabled", async () => {
    const user = userEvent.setup();
    const ref = createRef<HTMLInputElement>();
    const onChange = vi.fn();
    render(<Input ref={ref} type="number" aria-label="Limit" suffix="MB" disabled onChange={onChange} />);
    const input = screen.getByRole("spinbutton", { name: "Limit" });
    expect(ref.current).toBe(input);
    expect(screen.getByText("MB")).toBeTruthy();
    await user.type(input, "5");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Select", () => {
  it("selects an option", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Select aria-label="Format" defaultValue="mp3" onChange={(e) => onChange(e.target.value)}>
        <option value="mp3">MP3</option>
        <option value="flac">FLAC</option>
      </Select>,
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Format" }), "FLAC");
    expect(onChange).toHaveBeenCalledWith("flac");
  });
});

describe("Field", () => {
  it("labels its control and shows aside and hint", () => {
    render(
      <Field label="Folder" htmlFor="folder" hint="Where downloads go" aside="12 GB free">
        <Input id="folder" />
      </Field>,
    );
    expect(screen.getByRole("textbox", { name: "Folder" })).toBeTruthy();
    expect(screen.getByText("Where downloads go")).toBeTruthy();
    expect(screen.getByText("12 GB free")).toBeTruthy();
  });

  it("shows the error instead of the hint", () => {
    render(
      <Field label="Folder" hint="Where downloads go" error="Not writable">
        <Input />
      </Field>,
    );
    expect(screen.getByText("Not writable")).toBeTruthy();
    expect(screen.queryByText("Where downloads go")).toBeNull();
  });
});

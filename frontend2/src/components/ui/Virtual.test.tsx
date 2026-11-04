// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Table, TableScroll, TBody, Td, Tr } from "./Table";
import { ROW_H, VirtualList, VirtualRows } from "./Virtual";

afterEach(cleanup);

// jsdom has no layout: give every element a 300px viewport so the virtualiser has something to fill
const VIEW = 300;
const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => VIEW });
});
afterAll(() => {
  if (desc) Object.defineProperty(HTMLElement.prototype, "clientHeight", desc);
});

async function scrollTo(el: HTMLElement, top: number) {
  el.scrollTop = top;
  await act(async () => {
    fireEvent.scroll(el);
    await new Promise((r) => requestAnimationFrame(r));
  });
}

describe("VirtualList", () => {
  it("renders only the visible items plus overscan in a sized list", () => {
    render(<VirtualList aria-label="Jobs" count={1000} itemHeight={30} overscan={2} item={(i) => `job ${i}`} />);
    const list = screen.getByRole("list", { name: "Jobs" });
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(VIEW / 30 + 2);
    expect(items[0].textContent).toBe("job 0");
    expect((list.firstElementChild as HTMLElement).style.height).toBe("30000px");
  });

  it("renders the items scrolled into view", async () => {
    render(<VirtualList aria-label="Jobs" count={1000} itemHeight={30} overscan={0} item={(i) => `job ${i}`} />);
    await scrollTo(screen.getByRole("list"), 3000);
    const items = screen.getAllByRole("listitem");
    expect(items[0].textContent).toBe("job 100");
    expect(items[0].style.transform).toBe("translateY(3000px)");
    expect(screen.queryByText("job 0")).toBeNull();
  });

  it("renders nothing for an empty list", () => {
    render(<VirtualList count={0} itemHeight={30} item={() => null} />);
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });
});

function Rows({ count }: { count: number }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <TableScroll ref={ref}>
      <Table>
        <TBody>
          <VirtualRows
            scrollRef={ref}
            count={count}
            colSpan={2}
            overscan={0}
            rowKey={(i) => `id${i}`}
            row={(i) => (
              <Tr>
                <Td>row {i}</Td>
                <Td>x</Td>
              </Tr>
            )}
          />
        </TBody>
      </Table>
    </TableScroll>
  );
}

describe("VirtualRows", () => {
  it("renders the visible rows with a bottom spacer keeping the scroll height", () => {
    const { container } = render(<Rows count={500} />);
    const rows = [...container.querySelectorAll("tr:not([aria-hidden])")];
    expect(rows).toHaveLength(VIEW / ROW_H);
    expect(rows[0].textContent).toBe("row 0x");
    const spacers = container.querySelectorAll<HTMLElement>("tr[aria-hidden]");
    expect(spacers).toHaveLength(1);
    expect(spacers[0].style.height).toBe(`${(500 - VIEW / ROW_H) * ROW_H}px`);
    expect(spacers[0].querySelector("td")?.colSpan).toBe(2);
  });

  it("adds a top spacer once scrolled", async () => {
    const { container } = render(<Rows count={500} />);
    await scrollTo(container.firstElementChild as HTMLElement, 100 * ROW_H);
    const spacers = container.querySelectorAll<HTMLElement>("tr[aria-hidden]");
    expect(spacers).toHaveLength(2);
    expect(spacers[0].style.height).toBe(`${100 * ROW_H}px`);
    expect(container.querySelector("tr:not([aria-hidden])")?.textContent).toBe("row 100x");
  });

  it("renders no spacers when everything fits", () => {
    const { container } = render(<Rows count={3} />);
    expect(container.querySelectorAll("tr[aria-hidden]")).toHaveLength(0);
    expect(container.querySelectorAll("tr")).toHaveLength(3);
  });
});

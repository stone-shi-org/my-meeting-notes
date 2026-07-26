import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Pagination } from '../ui/states';

function setup(overrides: Partial<React.ComponentProps<typeof Pagination>> = {}) {
  const onPage = vi.fn();
  render(
    <Pagination
      page={1}
      pageSize={10}
      total={25}
      totalPages={3}
      onPage={onPage}
      {...overrides}
    />,
  );
  return { onPage };
}

describe('Pagination', () => {
  it('reports the visible range', () => {
    setup();
    expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 1–10 of 25');
  });

  it('clamps the last page to the total', () => {
    setup({ page: 3 });
    expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 21–25 of 25');
  });

  it('disables Prev on the first page', () => {
    setup({ page: 1 });
    expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('disables Next on the last page', () => {
    setup({ page: 3 });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('marks the current page for assistive tech', () => {
    setup({ page: 2 });
    expect(screen.getByRole('button', { current: 'page' })).toHaveTextContent('2');
  });

  it('renders nothing when there is nothing to page', () => {
    const { container } = render(
      <Pagination page={1} pageSize={10} total={0} totalPages={1} onPage={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('collapses a long run of pages with an ellipsis', () => {
    setup({ page: 10, totalPages: 20, total: 200 });
    expect(screen.getAllByText('…').length).toBeGreaterThan(0);
    // First and last are always reachable.
    expect(screen.getByRole('button', { name: '1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '20' })).toBeInTheDocument();
  });

  it('emits the requested page', async () => {
    const user = userEvent.setup();
    const { onPage } = setup({ page: 2 });

    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(onPage).toHaveBeenCalledWith(3);

    await user.click(screen.getByRole('button', { name: 'Prev' }));
    expect(onPage).toHaveBeenCalledWith(1);
  });
});

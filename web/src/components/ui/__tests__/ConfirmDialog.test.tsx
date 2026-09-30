import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { ConfirmDialog } from '../ConfirmDialog';

function TestWrapper(props: Partial<React.ComponentProps<typeof ConfirmDialog>>) {
  const [open, setOpen] = useState(true);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title="Test Title"
      description="Test Description"
      confirmLabel="Yes, do it"
      cancelLabel="No, keep it"
      onConfirm={vi.fn()}
      {...props}
    />
  );
}

describe('ConfirmDialog', () => {
  it('renders title, description and action buttons when open', () => {
    render(<TestWrapper />);

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Test Title')).toBeInTheDocument();
    expect(screen.getByText('Test Description')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Yes, do it' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'No, keep it' })).toBeInTheDocument();
  });

  it('calls onConfirm when confirm button is clicked', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<TestWrapper onConfirm={onConfirm} />);

    await user.click(screen.getByRole('button', { name: 'Yes, do it' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('calls onCancel and closes when cancel button is clicked', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(<TestWrapper onCancel={onCancel} />);

    await user.click(screen.getByRole('button', { name: 'No, keep it' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

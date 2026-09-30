import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Button } from './Button';
import { Card } from './primitives';
import { cn } from '@/lib/cn';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'danger' | 'primary' | 'secondary';
  loading?: boolean;
  onConfirm: () => void | Promise<void>;
  onCancel?: () => void;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'danger',
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <DialogPrimitive.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          onCancel?.();
        }
        onOpenChange(next);
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay backdrop-blur-sm data-[state=open]:animate-fade-in" />
        <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto p-4">
          <DialogPrimitive.Content
            className={cn(
              'w-full max-w-md outline-none data-[state=open]:animate-fade-in',
            )}
          >
            <Card className="p-6 shadow-lg">
              <DialogPrimitive.Title className="font-display text-xl font-semibold">
                {title}
              </DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description className="mt-2 text-sm text-fg-subtle leading-relaxed">
                  {description}
                </DialogPrimitive.Description>
              )}
              <div className="mt-6 flex justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  disabled={loading}
                  onClick={() => {
                    onCancel?.();
                    onOpenChange(false);
                  }}
                >
                  {cancelLabel}
                </Button>
                <Button
                  type="button"
                  variant={variant}
                  loading={loading}
                  onClick={async () => {
                    await onConfirm();
                  }}
                >
                  {confirmLabel}
                </Button>
              </div>
            </Card>
          </DialogPrimitive.Content>
        </div>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import type { VersionInfo } from '@/types/api';

/** Static in-flow footer, not a fixed overlay -- JobDock, the chat panels and
 * PlayerBar already compete for fixed screen-bottom real estate. */
export function Footer() {
  const version = useQuery({
    queryKey: ['version'],
    queryFn: () => api.get<VersionInfo>('/version'),
    staleTime: Infinity,
  });

  return (
    <footer className="mx-auto max-w-[1600px] px-4 py-4 text-center text-xs text-fg-faint sm:px-6 lg:px-8">
      {version.data && (
        <span>
          Version {version.data.hash}
          {version.data.timestamp && (
            <>
              {' · built '}
              <time dateTime={version.data.timestamp}>
                {new Date(version.data.timestamp).toLocaleString()}
              </time>
            </>
          )}
        </span>
      )}
    </footer>
  );
}

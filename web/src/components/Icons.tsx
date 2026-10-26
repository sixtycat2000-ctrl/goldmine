type IconProps = { size?: number; className?: string };

const S = (size?: number) => ({ width: size ?? 16, height: size ?? 16 });

export function IconLogo({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2" transform="rotate(45 12 12)" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="12" cy="9.5" r="1.8" fill="currentColor" />
    </svg>
  );
}

export function IconExt({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconCopy({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M5 15V6a2 2 0 0 1 2-2h9" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconYes({ size, className, label }: IconProps & { label: string }) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-label={label} role="img">
      <circle cx="9" cy="9" r="8" fill="currentColor" />
      <path d="M5.2 9.4l2.5 2.5 5-5.6" fill="none" stroke="#0c0c10" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconOk({ size, className, label }: IconProps & { label: string }) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-label={label} role="img">
      <path d="M4.5 9.6l3 3 6-6.6" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function IconNo({ size, className, label }: IconProps & { label: string }) {
  return (
    <svg {...S(size)} viewBox="0 0 18 18" className={className} aria-label={label} role="img">
      <circle cx="9" cy="9" r="7.2" fill="none" stroke="currentColor" strokeWidth="1.2" />
      <path d="M6.5 6.5l5 5M11.5 6.5l-5 5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

export function IconSearch({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M15.8 15.8 20 20" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export function IconClose({ size, className }: IconProps) {
  return (
    <svg {...S(size)} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="M6 6l12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

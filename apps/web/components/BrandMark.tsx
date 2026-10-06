// Marca: una hoja de libro mayor con un renglón resaltado.
export default function BrandMark({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 30 30" fill="none" aria-hidden="true" focusable="false">
      <rect x="1.2" y="1.2" width="27.6" height="27.6" rx="6.5" fill="#fcfefa" stroke="#16282b" strokeWidth="2" />
      <rect x="5.5" y="8.2" width="15.5" height="6.4" rx="1.4" fill="#ffe55c" />
      <path d="M6.5 11.4h17M6.5 17h17M6.5 22.2h10.5" stroke="#16282b" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

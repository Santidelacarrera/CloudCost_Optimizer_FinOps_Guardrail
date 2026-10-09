"use client";
// Puente entre el JSON de la API (todo en base64url) y la API WebAuthn del navegador (ArrayBuffer).
import { ApiError } from "@/lib/api";

type Json = Record<string, any>;

export const passkeysSupported = () =>
  typeof window !== "undefined" && typeof window.PublicKeyCredential !== "undefined" && typeof navigator !== "undefined" && !!navigator.credentials;

function fromB64u(text: string): ArrayBuffer {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const view = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
  return buf;
}

function toB64u(buf: ArrayBuffer): string {
  let bin = "";
  new Uint8Array(buf).forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const descriptors = (list: Json[] | undefined): PublicKeyCredentialDescriptor[] =>
  (list ?? []).map((c) => ({ type: "public-key", id: fromB64u(c.id), transports: c.transports }));

/** Crea una llave de acceso con las opciones que entregó la API y devuelve la credencial lista para enviarle. */
export async function createPasskey(options: Json): Promise<Json> {
  const publicKey: PublicKeyCredentialCreationOptions = {
    ...options,
    challenge: fromB64u(options.challenge),
    user: { ...options.user, id: fromB64u(options.user.id) },
    excludeCredentials: descriptors(options.excludeCredentials),
  } as PublicKeyCredentialCreationOptions;
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("cancelled");
  const r = cred.response as AuthenticatorAttestationResponse;
  return {
    id: cred.id,
    response: {
      clientDataJSON: toB64u(r.clientDataJSON),
      attestationObject: toB64u(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
  };
}

/** Pide la firma de una llave de acceso (con o sin lista de credenciales permitidas). */
export async function getPasskey(options: Json): Promise<Json> {
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: fromB64u(options.challenge),
    allowCredentials: descriptors(options.allowCredentials),
  } as PublicKeyCredentialRequestOptions;
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("cancelled");
  const r = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    response: {
      clientDataJSON: toB64u(r.clientDataJSON),
      authenticatorData: toB64u(r.authenticatorData),
      signature: toB64u(r.signature),
      userHandle: r.userHandle ? toB64u(r.userHandle) : null,
    },
  };
}

/** Texto para la persona: errores de la API tal cual; errores del navegador traducidos (cancelar es lo más común). */
export function passkeyErrorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  const name = (e as { name?: string })?.name;
  if (name === "NotAllowedError" || (e as Error)?.message === "cancelled") return "Se canceló o venció la solicitud de la llave de acceso. Inténtalo de nuevo.";
  if (name === "InvalidStateError") return "Esa llave de acceso ya está registrada en tu cuenta.";
  if (name === "NotSupportedError" || name === "SecurityError") return "Este navegador o este sitio no puede usar llaves de acceso.";
  return "No pudimos completar la operación con la llave de acceso.";
}

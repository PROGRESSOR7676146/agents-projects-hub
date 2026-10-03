export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'grammy') return {url:new URL('./mock-grammy.mjs', import.meta.url).href, shortCircuit:true};
  try { return await nextResolve(specifier, context); }
  catch (e) {
    if (e.code === 'ERR_MODULE_NOT_FOUND' && specifier.endsWith('.js')) return nextResolve(specifier.slice(0,-3)+'.ts',context);
    throw e;
  }
}

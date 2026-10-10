# Analizar gastos desde CSV o Excel

Sirve para revisar gastos **sin conectar ninguna cuenta**: subes el archivo y obtienes un informe. Funciona en la pantalla **Analizar gastos** del producto y desde la terminal.

```powershell
python scripts\expenses_analyze.py gastos-agosto.xlsx gastos-septiembre.xlsx cur-septiembre.csv --out-dir gastos-out
```
Genera `gastos-out\report.md` (el informe) y `gastos-out\result.json` (los datos). Varios meses se comparan entre sí.

## Qué archivos entiende
| Tipo | Ejemplos | Qué obtienes |
|---|---|---|
| **Gastos y presupuestos** (CSV o Excel) | gastos comunes, listados de costos, un Excel con «proveedor, concepto, importe» | si subtotales y totales cuadran, cobros repetidos, posibles pagos dobles, cobros atípicos, concentración, y con varios meses qué subió o apareció |
| **Estados de pago de obra** | avance real vs proyectado | atraso, anticipo, proyección de término |
| **Facturación de nube** | **AWS** CUR (CSV) y Cost Explorer (CSV de la consola, ancho); **Azure** Cost Management (CSV); **GCP** Billing export (CSV) | gasto por mes, servicio, región y cuenta; qué servicios subieron o aparecieron entre los dos últimos meses completos; picos diarios; créditos e impuestos |

Los **Excel (.xlsx)** se leen directamente (cada hoja con datos cuenta como un archivo). Se leen solo valores: las fórmulas se usan por su último resultado guardado y nunca se evalúan.

## Reglas de seguridad y límites
- No se aceptan libros con **macros** (.xlsm), archivos **.xls** antiguos, ni Excel que se expandan de forma sospechosa (bomba de compresión): hasta 9 MB comprimidos y 60 MB descomprimidos.
- Gastos/presupuestos: hasta 5 000 filas por hoja. Facturación de nube: la pantalla acepta hasta ~1,5 MB de CSV; desde la terminal, hasta 1 millón de filas (para un CUR grande, usa la terminal).
- El contenido del archivo **no se guarda** en el servidor ni en la auditoría: solo cantidades (archivos, hallazgos, períodos).
- **Nunca se suman monedas distintas**: un archivo de nube que mezcla monedas se rechaza con un mensaje pidiendo separarlo.

## Cómo leer el análisis de nube
- **Solo se comparan meses completos.** Si el archivo llega hasta el día 12 de octubre, octubre se muestra como «incompleto» y no se compara contra septiembre (sería una falsa bajada).
- **Qué cambió**: un servicio aparece como hallazgo si subió al menos el 5 % del gasto total del mes anterior y al menos 25 % respecto de sí mismo («Revisar primero» si subió ≥ 20 % del total). Un servicio nuevo se marca si pesa ≥ 3 % del mes.
- **Picos diarios** (solo con datos diarios): un día con ≥ 2,5× la mediana de los 14 días anteriores de ese servicio y ≥ 1 % del gasto total.
- **Créditos** e **impuestos** se informan aparte: el gasto neto puede ser menor que el gasto real mientras haya créditos.

## Lo que NO hace
- **No ve recursos individuales.** Una factura dice «EC2 subió 54 %», no «esta instancia está ociosa» ni cuánto se ahorraría. Para recomendaciones con ahorro estimado hay que conectar la cuenta o [importar el inventario](supported-services.md) (CSV o Excel, EC2/EBS/snapshots).
- No interpreta un export con otro formato de columnas (si no lo reconoce, lo trata como estado de gastos genérico y puede fallar con un mensaje).
- Los hallazgos son **pistas para revisar**, no conclusiones.

## Nivel de verificación
Probado con archivos ficticios con la forma documentada de cada proveedor (pruebas `test_expenses_cloud.py` y `test_expenses_api.py`). **No se ha probado con exportaciones reales** de AWS, Azure o GCP: los nombres de columna salen de la documentación de cada proveedor y pueden variar según la versión de la exportación. Si un export real no se reconoce, pásalo (anonimizado) para añadir su formato.

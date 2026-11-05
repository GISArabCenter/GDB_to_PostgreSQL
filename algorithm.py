# -*- coding: utf-8 -*-
"""
GDB -> GeoPackage -> PostgreSQL/PostGIS
by Mohamed Metwaly.

Tested design target: QGIS 4.x / GDAL OpenFileGDB.
Install as a Processing script in the QGIS Processing scripts folder.
"""

import os
import xml.etree.ElementTree as ET
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingException,
    QgsVectorLayer,
    QgsProject,
    QgsEditorWidgetSetup,
    QgsField,
    QgsDefaultValue,
    QgsCoordinateReferenceSystem,
    QgsDataSourceUri,
    QgsVectorFileWriter,
    QgsMapLayer,
)
from qgis.PyQt.QtCore import QVariant
from osgeo import gdal, ogr


class GDBToPostgresWithDomainsAndStyles(QgsProcessingAlgorithm):

    INPUT_GDB = "INPUT_GDB"
    OUTPUT_GPKG = "OUTPUT_GPKG"
    PG_HOST = "PG_HOST"
    PG_PORT = "PG_PORT"
    PG_DATABASE = "PG_DATABASE"
    PG_SCHEMA = "PG_SCHEMA"
    PG_USER = "PG_USER"
    PG_PASSWORD = "PG_PASSWORD"
    ADD_TO_PROJECT = "ADD_TO_PROJECT"

    def name(self):
        return "gdb_to_postgres_domains_styles"

    def displayName(self):
        return "GDB to PostgreSQL"

    def group(self):
        return "GIS Data Conversion"

    def groupId(self):
        return "gis_data_conversion"

    def shortHelpString(self):
        return (
            "<h3>GDB to PostgreSQL/PostGIS</h3>"
            "<p><b>Developed by:</b>  Mohamed Metwaly</p>"
            "<p><b>Conversion Workflow:</b></p>"
            "<ol>"
            "<li><b>GDB to GeoPackage:</b> Convert all feature classes "
            "into an intermediate GeoPackage.</li>"
            "<li><b>GeoPackage to PostgreSQL:</b> Transfer spatial layers "
            "and attributes to the target database schema.</li>"
            "<li><b>Post-Processing:</b> Configure coded-value domains, "
            "FCSubtype mappings, editor tracking, CRS/SRID, and QGIS "
            "layer styles. Remove the original ObjectID field.</li>"
            "</ol>"
        )

    def createInstance(self):
        return GDBToPostgresWithDomainsAndStyles()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_GDB,
                "Input File Geodatabase (.gdb folder)",
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_GPKG,
                "Intermediate GeoPackage",
                "GeoPackage (*.gpkg)",
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PG_HOST, "PostgreSQL host",
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PG_PORT,
                "PostgreSQL port",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=5432,
                minValue=1,
                maxValue=65535,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PG_DATABASE, "Database name"
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PG_SCHEMA, "Target schema", defaultValue="public"
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PG_USER, "PostgreSQL username"
            )
        )
        password_param = QgsProcessingParameterString(
            self.PG_PASSWORD, "PostgreSQL password", multiLine=False
        )
        self.addParameter(password_param)

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ADD_TO_PROJECT,
                "Add imported PostgreSQL layers to current QGIS project (0=No, 1=Yes)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=0,
                maxValue=1,
            )
        )

    @staticmethod
    def _safe_layer_name(name):
        # Preserve the original layer/table name as far as possible.
        # PostgreSQL quoted identifiers support mixed case and spaces, but
        # conservative normalization avoids provider and SQL naming problems.
        value = "".join(
            ch if (ch.isalnum() or ch == "_") else "_"
            for ch in str(name)
        )
        if not value:
            value = "layer"
        if value[0].isdigit():
            value = "lyr_" + value
        return value[: POSTGRESQL_IDENTIFIER_LIMIT]

    @staticmethod
    def _domain_map(domain_obj):
        """Return QGIS ValueMap list: [{'description': code}, ...]."""
        try:
            enum = domain_obj.GetEnumeration()
        except Exception:
            return []

        if not enum:
            return []

        items = list(enum.items()) if hasattr(enum, "items") else list(enum)
        value_map = []
        for item in items:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                code, description = item[0], item[1]
            else:
                code, description = item, item

            if description is None or str(description) == "":
                description = str(code)

            value_map.append({str(description): code})

        return value_map

    @staticmethod
    def _read_subtype_mappings(gdb_path, feedback):
        """Read ArcGIS subtype codes/names from GDB_Items XML, without editing GDB."""
        def local(tag):
            return tag.rsplit("}", 1)[-1].lower()

        def nested_text(node, names):
            names = {name.lower() for name in names}
            for element in node.iter():
                if element is not node and local(element.tag) in names and element.text and element.text.strip():
                    return element.text.strip()
            return None

        mappings = {}
        ds = None
        try:
            ds = gdal.OpenEx(
                gdb_path, gdal.OF_VECTOR,
                allowed_drivers=["OpenFileGDB"],
                open_options=["LIST_ALL_TABLES=YES"]
            )
            if ds is None:
                return mappings
            items = ds.GetLayerByName("GDB_Items")
            if items is None:
                feedback.pushWarning("GDB_Items not exposed; subtype XML fallback unavailable")
                return mappings
            for feature in items:
                xml = feature.GetField("Definition")
                if not isinstance(xml, str) or "Subtype" not in xml:
                    continue
                try:
                    root = ET.fromstring(xml)
                except ET.ParseError:
                    continue
                pairs = {}
                for element in root.iter():
                    if local(element.tag) != "subtype":
                        continue
                    code = nested_text(element, ("SubtypeCode", "Code"))
                    label = nested_text(element, ("SubtypeName", "Name"))
                    if code is not None and label:
                        try:
                            pairs[int(code)] = label
                        except ValueError:
                            continue
                if pairs:
                    layer_name = nested_text(root, ("Name",))
                    field_name = nested_text(root, ("SubtypeFieldName", "SubtypeField"))
                    if layer_name and (not field_name or field_name.lower() == "fcsubtype"):
                        key = layer_name.replace("\\", "/").rsplit("/", 1)[-1].lower()
                        mappings[key] = [{label: code} for code, label in sorted(pairs.items())]
        except Exception as exc:
            feedback.pushWarning("Subtype XML read warning: " + str(exc))
        finally:
            ds = None
        return mappings

    @staticmethod
    def _create_pg_subtype_domain(host, port, database, username, password,
                                  schema, table, value_map, feedback):
        """Create a real PostgreSQL DOMAIN for FCSubtype, preserving numeric codes.

        The mapping is obtained from ArcGIS GDB domain or subtype XML. Existing
        target tables are already overwritten by the original upload routine.
        A domain is not dropped on reruns; existing domains are reused if their
        definitions match. Failure here does not undo a successful layer upload.
        """
        if not value_map:
            return False
        import psycopg2
        from psycopg2 import sql
        domain_name = (table[:43] + "_fcsubtype_dom").lower()
        with psycopg2.connect(host=host, port=port, dbname=database,
                              user=username, password=password) as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT column_name, data_type, udt_name
                               FROM information_schema.columns
                               WHERE table_schema=%s AND table_name=%s
                                 AND lower(column_name)='fcsubtype'""", (schema, table))
                col = cur.fetchone()
                if not col:
                    feedback.pushWarning("No FCSubtype column in PostgreSQL: " + table)
                    return False
                col_name, data_type, udt_name = col
                if data_type not in ("smallint", "integer", "bigint", "text", "character varying", "numeric"):
                    feedback.pushWarning("FCSubtype has unsupported type: " + data_type)
                    return False
                base_type = "text" if data_type in ("text", "character varying") else data_type
                codes = []
                for entry in value_map:
                    code = next(iter(entry.values()))
                    try:
                        if base_type == "numeric":
                            from decimal import Decimal
                            numeric_code = Decimal(str(code))
                            if not numeric_code.is_finite() or numeric_code != numeric_code.to_integral_value():
                                raise ValueError("FCSubtype code must be an integer")
                            codes.append(numeric_code)
                        else:
                            codes.append(str(code) if base_type == "text" else int(code))
                    except (ValueError, TypeError, ArithmeticError):
                        feedback.pushWarning("Invalid FCSubtype code for " + table)
                        return False
                codes = list(dict.fromkeys(codes))
                if not codes:
                    return False
                # Never silently reject pre-existing values in the uploaded data.
                cur.execute(sql.SQL("SELECT DISTINCT {} FROM {}.{} WHERE {} IS NOT NULL").format(
                    sql.Identifier(col_name), sql.Identifier(schema), sql.Identifier(table),
                    sql.Identifier(col_name)))
                present = {str(row[0]) for row in cur.fetchall()}
                if base_type == "numeric":
                    from decimal import Decimal
                    missing = {value for value in present if Decimal(value) not in codes}
                else:
                    missing = present - {str(x) for x in codes}
                if missing:
                    feedback.pushWarning("FCSubtype domain skipped for {}: codes absent from "
                                         "GDB definition: {}".format(table, ", ".join(sorted(missing))))
                    return False
                cur.execute("""SELECT t.typname, pg_catalog.format_type(t.typbasetype, NULL)
                               FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                               WHERE n.nspname=%s AND t.typname=%s AND t.typtype='d'""",
                            (schema, domain_name))
                existing = cur.fetchone()
                if existing:
                    # The same domain may be referenced by previous tables.
                    # Do not modify or drop it; only attach when codes match.
                    cur.execute("""SELECT pg_get_constraintdef(c.oid)
                                   FROM pg_constraint c JOIN pg_type t ON t.oid=c.contypid
                                   JOIN pg_namespace n ON n.oid=t.typnamespace
                                   WHERE n.nspname=%s AND t.typname=%s AND c.contype='c'""",
                                (schema, domain_name))
                    constraints = [r[0] for r in cur.fetchall()]
                    if existing[1] != base_type or not constraints:
                        feedback.pushWarning("Existing subtype domain differs: " + domain_name)
                        return False
                    # Existing constraints may not reflect new GDB codes; avoid
                    # altering them silently. Let PostgreSQL validate conversion.
                else:
                    allowed = sql.SQL(", ").join(sql.Literal(x) for x in codes)
                    cur.execute(sql.SQL("CREATE DOMAIN {}.{} AS {} CHECK (VALUE IN ({}))").format(
                        sql.Identifier(schema), sql.Identifier(domain_name),
                        sql.SQL(base_type), allowed))
                cur.execute(sql.SQL("ALTER TABLE {}.{} ALTER COLUMN {} TYPE {}.{} "
                                    "USING {}::{}::{}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(col_name),
                    sql.Identifier(schema), sql.Identifier(domain_name),
                    sql.Identifier(col_name), sql.SQL(base_type),
                    sql.Identifier(schema), sql.Identifier(domain_name)))
        feedback.pushInfo("PostgreSQL FCSubtype DOMAIN attached: {}.{} -> {} ({} codes)".format(
            schema, table, domain_name, len(codes)))
        return True

    @staticmethod
    def _load_style(layer, qml_path):
        """
        QGIS loadNamedStyle normally returns (message, success_boolean).
        Return (success, message) and tolerate minor API return variations.
        """
        result = layer.loadNamedStyle(qml_path)

        if isinstance(result, tuple):
            if len(result) >= 2:
                message, ok = result[0], result[1]
                return bool(ok), str(message)
            if len(result) == 1:
                return bool(result[0]), ""
        return bool(result), ""

    @staticmethod
    def _save_style_to_database(layer):
        """
        Save as database default style. QGIS versions expose the result as
        (success, message). Return handling is defensive.
        """
        result = layer.saveStyleToDatabaseV2(
            "default",
            "Imported from ArcGIS File Geodatabase",
            True,
            None,
        )

        if isinstance(result, tuple):
            if len(result) >= 2:
                ok, message = result[0], result[1]
                return bool(ok), str(message)
            if len(result) == 1:
                return bool(result[0]), ""
        return bool(result), ""

    @staticmethod
    def _configure_audit_fields(layer, feedback):
        """Add missing fields and configure QGIS Editor Tracking expressions."""
        if not layer.isValid():
            raise Exception("Invalid layer while configuring audit fields.")

        if not layer.startEditing():
            raise Exception("Could not start editing layer: " + layer.name())

        existing = {field.name().lower(): field.name() for field in layer.fields()}
        additions = []

        # Retain an existing GlobalID/Global_ID field; add global_id only if absent.
        has_global_id = any(
            key in ("global_id", "globalid", "global_id") for key in existing
        )
        if not has_global_id:
            additions.append(QgsField("global_id", QVariant.String, "string", 36))

        if "create_user" not in existing:
            additions.append(QgsField("create_user", QVariant.String, "string", 100))
        if "create_date" not in existing:
            additions.append(QgsField("create_date", QVariant.DateTime, "datetime"))
        if "last_edit_user" not in existing:
            additions.append(QgsField("last_edit_user", QVariant.String, "string", 100))
        if "last_edit_date" not in existing:
            additions.append(QgsField("last_edit_date", QVariant.DateTime, "datetime"))

        if additions:
            for new_field in additions:
                if not layer.addAttribute(new_field):
                    layer.rollBack()
                    raise Exception("Could not add field {} to {}".format(new_field.name(), layer.name()))

        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            layer.rollBack()
            raise Exception("Could not commit audit fields: " + errors)

        layer.updateFields()

        for field in layer.fields():
            field_name = field.name().lower()
            index = layer.fields().indexFromName(field.name())

            if field_name in ("global_id", "globalid", "global_id"):
                layer.setEditorWidgetSetup(
                    index, QgsEditorWidgetSetup("UuidGenerator", {})
                )
                layer.setDefaultValueDefinition(
                    index, QgsDefaultValue("uuid()", False)
                )

            elif field_name == "create_user":
                layer.setDefaultValueDefinition(
                    index, QgsDefaultValue("@user_account_name", False)
                )

            elif field_name == "create_date":
                layer.setDefaultValueDefinition(
                    index, QgsDefaultValue("now()", False)
                )

            elif field_name == "last_edit_user":
                layer.setDefaultValueDefinition(
                    index, QgsDefaultValue("@user_account_name", True)
                )

            elif field_name == "last_edit_date":
                layer.setDefaultValueDefinition(
                    index, QgsDefaultValue("now()", True)
                )

    def processAlgorithm(self, parameters, context, feedback):
        gdb_path = self.parameterAsFile(parameters, self.INPUT_GDB, context)
        gpkg_path = self.parameterAsFileOutput(parameters, self.OUTPUT_GPKG, context)

        host = self.parameterAsString(parameters, self.PG_HOST, context).strip()
        port = self.parameterAsInt(parameters, self.PG_PORT, context)
        database = self.parameterAsString(parameters, self.PG_DATABASE, context).strip()
        schema = self.parameterAsString(parameters, self.PG_SCHEMA, context).strip()
        username = self.parameterAsString(parameters, self.PG_USER, context).strip()
        password = self.parameterAsString(parameters, self.PG_PASSWORD, context)
        add_to_project = self.parameterAsInt(parameters, self.ADD_TO_PROJECT, context)

        if not gdb_path or not os.path.isdir(gdb_path):
            raise QgsProcessingException("The selected GDB path is not a valid folder.")
        if not database or not schema or not username:
            raise QgsProcessingException("Database, schema and username are required.")
        if not gpkg_path.lower().endswith(".gpkg"):
            gpkg_path += ".gpkg"
        if os.path.exists(gpkg_path):
            raise QgsProcessingException(
                "The output GeoPackage already exists. Delete it or choose another path."
            )

        os.makedirs(os.path.dirname(gpkg_path) or ".", exist_ok=True)
        qml_folder = os.path.splitext(gpkg_path)[0] + "_QML"
        os.makedirs(qml_folder, exist_ok=True)

        gdal.UseExceptions()
        feedback.pushInfo("Opening File Geodatabase...")
        src_ds = ogr.Open(gdb_path, 0)
        if src_ds is None:
            raise QgsProcessingException("GDAL could not open the selected GDB.")

        layer_names = [
            src_ds.GetLayerByIndex(i).GetName()
            for i in range(src_ds.GetLayerCount())
        ]
        if not layer_names:
            src_ds = None
            raise QgsProcessingException("No layers were found in the GDB.")

        # 1) Read coded-value domains.
        feedback.pushInfo("Reading coded-value domains...")
        domains_dict = {}
        try:
            domain_names = src_ds.GetFieldDomainNames() or []
            for domain_name in domain_names:
                domain_obj = src_ds.GetFieldDomain(domain_name)
                if domain_obj is None:
                    continue
                value_map = self._domain_map(domain_obj)
                if value_map:
                    domains_dict[domain_name] = value_map
                    domains_dict[domain_name.lower()] = value_map
        except Exception as exc:
            feedback.pushWarning("Domain extraction warning: " + str(exc))

        feedback.pushInfo(
            "Unique coded-value domains read: {}".format(len(domains_dict) // 2)
        )

        subtype_maps = self._read_subtype_mappings(gdb_path, feedback)
        feedback.pushInfo("Subtype mappings read: {}".format(len(subtype_maps)))

        # 2) Convert every GDB layer into one GeoPackage.
        feedback.pushInfo("Converting GDB layers to GeoPackage...")
        converted = []
        for i, source_name in enumerate(layer_names):
            if feedback.isCanceled():
                src_ds = None
                raise QgsProcessingException("Operation canceled.")

            try:
                options = gdal.VectorTranslateOptions(
                    format="GPKG",
                    layers=[source_name],
                    accessMode="update" if i > 0 else None,
                )
                translated = gdal.VectorTranslate(
                    gpkg_path, gdb_path, options=options
                )
                if translated is not None or os.path.exists(gpkg_path):
                    converted.append(source_name)
                    feedback.pushInfo("Converted: " + source_name)
                translated = None
            except Exception as exc:
                feedback.pushWarning(
                    "Conversion failed for {}: {}".format(source_name, exc)
                )

        if not converted:
            src_ds = None
            raise QgsProcessingException("No GDB layers could be converted.")

        # Prepare every GeoPackage layer before one PostgreSQL batch upload.
        feedback.pushInfo("Preparing GeoPackage fields for batch upload...")
        for source_name in converted:
            prep_layer = QgsVectorLayer(
                gpkg_path + "|layername=" + source_name, source_name, "ogr"
            )
            if not prep_layer.isValid():
                raise QgsProcessingException("Cannot prepare GeoPackage layer: " + source_name)
            self._configure_audit_fields(prep_layer, feedback)
            del prep_layer

        # Upload the complete GeoPackage with one GDAL PostgreSQL operation.
        from psycopg2.extensions import make_dsn
        pg_destination = "PG:" + make_dsn(
            host=host, port=str(port), dbname=database,
            user=username, password=password
        )
        feedback.pushInfo("Batch uploading {} layers to PostgreSQL...".format(len(converted)))
        try:
            batch_options = gdal.VectorTranslateOptions(
                format="PostgreSQL",
                accessMode="overwrite",
                layerCreationOptions=[
                    "SCHEMA=" + schema,
                    "GEOMETRY_NAME=geom",
                    "FID=ogc_fid",
                    "LAUNDER=NO",
                    "PRECISION=YES",
                ],
            )
            batch_result = gdal.VectorTranslate(
                pg_destination, gpkg_path, options=batch_options
            )
            if batch_result is None:
                raise RuntimeError(gdal.GetLastErrorMsg() or "GDAL returned no dataset")
            batch_result = None  # Close GDAL PostgreSQL connection.
        except Exception as exc:
            raise QgsProcessingException("PostgreSQL batch upload failed: " + str(exc))

        # Match original script's normalized PostgreSQL table names.
        import psycopg2
        from psycopg2 import sql
        with psycopg2.connect(host=host, port=port, dbname=database,
                              user=username, password=password) as rename_conn:
            with rename_conn.cursor() as rename_cur:
                for source_name in converted:
                    table_name = self._safe_layer_name(source_name)
                    if table_name != source_name:
                        rename_cur.execute(sql.SQL(
                            "ALTER TABLE {}.{} RENAME TO {}"
                        ).format(sql.Identifier(schema), sql.Identifier(source_name),
                                 sql.Identifier(table_name)))
        feedback.pushInfo("PostgreSQL batch upload completed.")

        # Build PostgreSQL connection URI.
        pg_uri = QgsDataSourceUri()
        pg_uri.setConnection(
            host, str(port), database, username, password
        )

        project = QgsProject.instance()
        total_widgets = 0
        uploaded_count = 0
        style_count = 0
        failed_count = 0
        heuristic_count = 0

        # 3) Configure fields and Value Maps in GPKG, then upload to Postgres.
        for index, source_name in enumerate(converted):
            if feedback.isCanceled():
                src_ds = None
                raise QgsProcessingException("Operation canceled.")

            feedback.pushInfo("")
            feedback.pushInfo("Processing: " + source_name)

            table_name = self._safe_layer_name(source_name)
            gpkg_layer = QgsVectorLayer(
                gpkg_path + "|layername=" + source_name,
                source_name,
                "ogr",
            )

            if not gpkg_layer.isValid():
                feedback.pushWarning("Could not open GPKG layer: " + source_name)
                failed_count += 1
                continue

            try:
                # Read the original CRS from THIS GDB feature class (not a fixed EPSG).
                source_for_crs = src_ds.GetLayerByName(source_name)
                source_srs = source_for_crs.GetSpatialRef() if source_for_crs is not None else None
                source_crs = QgsCoordinateReferenceSystem()
                source_srid = None
                if source_srs is not None:
                    source_crs = QgsCoordinateReferenceSystem.fromWkt(source_srs.ExportToWkt())
                    try:
                        source_srs.AutoIdentifyEPSG()
                        authority = source_srs.GetAuthorityName(None)
                        code = source_srs.GetAuthorityCode(None)
                        if authority and authority.upper() == "EPSG" and code:
                            source_srid = int(code)
                    except Exception:
                        pass
                    if source_srid is None and source_crs.isValid():
                        try:
                            srid_candidate = int(source_crs.postgisSrid())
                            if srid_candidate > 0:
                                source_srid = srid_candidate
                        except Exception:
                            pass
                    if source_crs.isValid():
                        if not gpkg_layer.crs().isValid() or gpkg_layer.crs() != source_crs:
                            # Assignment only; no coordinate transformation.
                            gpkg_layer.setCrs(source_crs)
                    feedback.pushInfo("Original GDB CRS for {}: {}".format(
                        source_name, source_crs.authid() or "custom WKT"))
                else:
                    feedback.pushWarning("No source CRS in GDB: " + source_name)

                # Keep audit fields and restore Editor Tracking expressions.
                self._configure_audit_fields(gpkg_layer, feedback)

                # Configure coded-value domains as Value Map widgets.
                source_layer = src_ds.GetLayerByName(source_name)
                exact_links = 0
                fallback_links = 0

                if source_layer is not None:
                    source_defn = source_layer.GetLayerDefn()

                    for field_i in range(source_defn.GetFieldCount()):
                        source_field = source_defn.GetFieldDefn(field_i)
                        field_name = source_field.GetName()
                        target_index = gpkg_layer.fields().indexFromName(field_name)

                        if target_index < 0:
                            continue

                        assigned_map = None
                        domain_name = None

                        try:
                            domain_name = source_field.GetDomainName()
                        except Exception:
                            domain_name = None

                        # First choice: exact field -> domain assignment from GDB.
                        if domain_name:
                            assigned_map = (
                                domains_dict.get(domain_name)
                                or domains_dict.get(str(domain_name).lower())
                            )
                            if assigned_map:
                                exact_links += 1

                        # FCSubtype: use original subtype names from GDB XML when
                        # no field domain was exposed by GDAL. Preserve numeric codes.
                        if field_name.lower() == "fcsubtype" and not assigned_map:
                            assigned_map = subtype_maps.get(source_name.lower())
                            if assigned_map:
                                exact_links += 1
                                feedback.pushInfo("FCSubtype ValueMap from GDB XML: " + source_name)

                        # Retain original script's best-effort fallback.
                        # It is explicitly logged because name similarity is not proof.
                        if not assigned_map and field_name.lower() != "fcsubtype":
                            field_lower = field_name.lower()

                            if field_name in domains_dict:
                                assigned_map = domains_dict[field_name]
                            elif field_lower in domains_dict:
                                assigned_map = domains_dict[field_lower]
                            else:
                                candidates = []
                                for domain_key, candidate_map in domains_dict.items():
                                    if domain_key == domain_key.lower():
                                        continue
                                    key_lower = domain_key.lower()
                                    if len(key_lower) > 3 and (
                                        key_lower in field_lower
                                        or field_lower in key_lower
                                    ):
                                        candidates.append((domain_key, candidate_map))

                                if len(candidates) == 1:
                                    assigned_map = candidates[0][1]
                                    fallback_links += 1
                                    heuristic_count += 1

                        if assigned_map:
                            gpkg_layer.setEditorWidgetSetup(
                                target_index,
                                QgsEditorWidgetSetup(
                                    "ValueMap", {"map": assigned_map}
                                ),
                            )
                            total_widgets += 1

                feedback.pushInfo(
                    "Value Maps: {} exact, {} heuristic fallback".format(
                        exact_links, fallback_links
                    )
                )

                # Save the QML after all field widgets/defaults have been configured.
                qml_path = os.path.join(qml_folder, table_name + ".qml")
                qml_message, qml_saved = gpkg_layer.saveNamedStyle(qml_path)
                if not qml_saved:
                    feedback.pushWarning(
                        "QML export warning for {}: {}".format(
                            source_name, qml_message
                        )
                    )

                # Already uploaded in the single PostgreSQL batch above.
                feedback.pushInfo("PostgreSQL batch layer: " + table_name)

                uploaded_count += 1

                # Remove SQL column DEFAULT clauses (but keep primary-key identity
                # mechanisms required by the PostgreSQL driver for ogc_fid).
                # This also clears any defaults copied from the original GDB.
                try:
                    import psycopg2
                    from psycopg2 import sql
                    with psycopg2.connect(host=host, port=port, dbname=database,
                                          user=username, password=password) as clean_conn:
                        with clean_conn.cursor() as clean_cur:
                            clean_cur.execute("""
                                SELECT a.attname, pg_get_expr(d.adbin, d.adrelid)
                                FROM pg_attribute a
                                JOIN pg_class t ON t.oid = a.attrelid
                                JOIN pg_namespace n ON n.oid = t.relnamespace
                                LEFT JOIN pg_attrdef d ON d.adrelid = t.oid
                                                      AND d.adnum = a.attnum
                                WHERE n.nspname = %s AND t.relname = %s
                                  AND a.attnum > 0 AND NOT a.attisdropped
                                  AND a.attidentity = ''
                                  AND NOT EXISTS (
                                      SELECT 1 FROM pg_index ix
                                      WHERE ix.indrelid = t.oid AND ix.indisprimary
                                        AND a.attnum = ANY(ix.indkey)
                                  )
                                  AND d.oid IS NOT NULL
                            """, (schema, table_name))
                            for col_name, _ in clean_cur.fetchall():
                                clean_cur.execute(sql.SQL(
                                    "ALTER TABLE {}.{} ALTER COLUMN {} DROP DEFAULT"
                                ).format(sql.Identifier(schema), sql.Identifier(table_name),
                                         sql.Identifier(col_name)))
                except Exception as defaults_error:
                    feedback.pushWarning("Could not clear PostgreSQL defaults for {}: {}".format(
                        table_name, defaults_error))

                # Set the PostgreSQL geometry SRID to the source GDB EPSG code.
                # UpdateGeometrySRID assigns metadata and ST_SRID; it does NOT
                # transform or change coordinate values.
                if source_srid is not None and source_srid > 0:
                    try:
                        import psycopg2
                        from psycopg2 import sql
                        with psycopg2.connect(host=host, port=port, dbname=database,
                                              user=username, password=password) as crs_conn:
                            with crs_conn.cursor() as crs_cur:
                                crs_cur.execute("""
                                    SELECT n.nspname FROM pg_proc p
                                    JOIN pg_namespace n ON n.oid = p.pronamespace
                                    WHERE lower(p.proname) = 'updategeometrysrid'
                                      AND p.pronargs = 4
                                    LIMIT 1
                                """)
                                row = crs_cur.fetchone()
                                if row is None:
                                    raise RuntimeError("PostGIS UpdateGeometrySRID is unavailable")
                                crs_cur.execute(sql.SQL(
                                    "SELECT {}.UpdateGeometrySRID(%s::text, %s::text, %s::text, %s::integer)"
                                ).format(sql.Identifier(row[0])),
                                    (schema, table_name, "geom", source_srid))
                        feedback.pushInfo("PostGIS SRID assigned from GDB: {} -> {}".format(
                            table_name, source_srid))
                    except Exception as crs_error:
                        feedback.pushWarning("CRS/SRID assignment failed for {}: {}".format(
                            table_name, crs_error))
                elif source_srs is not None:
                    feedback.pushWarning("Source CRS has no recognized PostGIS SRID for {}. "
                                         "No SRID was guessed.".format(table_name))

                # Attach a genuine PostgreSQL DOMAIN to FCSubtype, in addition
                # to its QGIS ValueMap. Keep all original upload/style behavior.
                fcsubtype_map = None
                if source_layer is not None:
                    for field_i in range(source_layer.GetLayerDefn().GetFieldCount()):
                        sf = source_layer.GetLayerDefn().GetFieldDefn(field_i)
                        if sf.GetName().lower() == "fcsubtype":
                            try:
                                dn = sf.GetDomainName()
                                fcsubtype_map = (domains_dict.get(dn) or
                                                 domains_dict.get(str(dn).lower())) if dn else None
                            except Exception:
                                pass
                            if not fcsubtype_map:
                                fcsubtype_map = subtype_maps.get(source_name.lower())
                            break
                if fcsubtype_map:
                    try:
                        self._create_pg_subtype_domain(
                            host, port, database, username, password,
                            schema, table_name, fcsubtype_map, feedback)
                    except Exception as domain_error:
                        feedback.pushWarning("PostgreSQL FCSubtype domain failed for {}: {}".format(
                            table_name, domain_error))
                # Remove the imported ObjectID column from PostgreSQL only.
                import psycopg2
                from psycopg2 import sql
                with psycopg2.connect(host=host, port=port, dbname=database,
                                      user=username, password=password) as objectid_conn:
                    with objectid_conn.cursor() as objectid_cur:
                        objectid_cur.execute("""
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = %s AND table_name = %s
                              AND lower(column_name) = 'objectid'
                        """, (schema, table_name))
                        for (objectid_column,) in objectid_cur.fetchall():
                            objectid_cur.execute(sql.SQL(
                                "ALTER TABLE {}.{} DROP COLUMN {}"
                            ).format(sql.Identifier(schema), sql.Identifier(table_name),
                                     sql.Identifier(objectid_column)))
                            feedback.pushInfo("Removed PostgreSQL column: {}.{}.{}".format(
                                schema, table_name, objectid_column))

                # Open uploaded PostgreSQL table as a QGIS layer.
                postgres_uri = QgsDataSourceUri()
                postgres_uri.setConnection(
                    host, str(port), database, username, password
                )
                postgres_uri.setDataSource(
                    schema, table_name, "geom", "", "ogc_fid"
                )

                pg_layer = QgsVectorLayer(
                    postgres_uri.uri(False),
                    table_name,
                    "postgres",
                )
                if not pg_layer.isValid():
                    raise Exception(
                        "Uploaded table exists but QGIS could not open it. "
                        "Check schema/table permissions and geometry column."
                    )

                # Apply QML to PostgreSQL layer (includes Value Map/editor settings).
                if os.path.exists(qml_path):
                    style_ok, style_message = self._load_style(pg_layer, qml_path)
                    if not style_ok:
                        feedback.pushWarning(
                            "Could not apply QML to {}: {}".format(
                                table_name, style_message
                            )
                        )
                    else:
                        feedback.pushInfo("QML applied to PostgreSQL layer.")

                # Re-apply audit defaults to the PostgreSQL layer explicitly.
                for field in pg_layer.fields():
                    field_name = field.name().lower()
                    field_index = pg_layer.fields().indexFromName(field.name())

                    if field_name in ("global_id", "globalid", "global_id"):
                        pg_layer.setEditorWidgetSetup(
                            field_index,
                            QgsEditorWidgetSetup("UuidGenerator", {}),
                        )
                        pg_layer.setDefaultValueDefinition(
                            field_index, QgsDefaultValue("uuid()", False)
                        )
                    elif field_name == "create_user":
                        pg_layer.setDefaultValueDefinition(
                            field_index,
                            QgsDefaultValue("@user_account_name", False),
                        )
                    elif field_name == "create_date":
                        pg_layer.setDefaultValueDefinition(
                            field_index, QgsDefaultValue("now()", False)
                        )
                    elif field_name == "last_edit_user":
                        pg_layer.setDefaultValueDefinition(
                            field_index,
                            QgsDefaultValue("@user_account_name", True),
                        )
                    elif field_name == "last_edit_date":
                        pg_layer.setDefaultValueDefinition(
                            field_index, QgsDefaultValue("now()", True)
                        )

                # Persist all available QGIS style categories as database default.
                db_style_ok, db_style_message = self._save_style_to_database(pg_layer)
                if db_style_ok:
                    style_count += 1
                    feedback.pushInfo(
                        "Saved PostgreSQL default style for: " + table_name
                    )
                else:
                    feedback.pushWarning(
                        "Style was applied but could not be saved in layer_styles "
                        "for {}: {}".format(table_name, db_style_message)
                    )

                pg_layer.triggerRepaint()
                if add_to_project:
                    project.addMapLayer(pg_layer)

            except Exception as exc:
                failed_count += 1
                feedback.pushWarning(
                    "ERROR processing {}: {}".format(source_name, str(exc))
                )

        src_ds = None

        feedback.pushInfo("")
        feedback.pushInfo("========== FINAL REPORT ==========")
        feedback.pushInfo("GDB layers found: {}".format(len(layer_names)))
        feedback.pushInfo("GPKG layers converted: {}".format(len(converted)))
        feedback.pushInfo("PostgreSQL layers uploaded: {}".format(uploaded_count))
        feedback.pushInfo("Value Map widgets configured: {}".format(total_widgets))
        feedback.pushInfo("Heuristic domain matches (review these): {}".format(heuristic_count))
        feedback.pushInfo("Default styles saved in PostgreSQL: {}".format(style_count))
        feedback.pushInfo("Layers with errors: {}".format(failed_count))
        feedback.pushInfo("GeoPackage: {}".format(gpkg_path))
        feedback.pushInfo("QML folder: {}".format(qml_folder))
        feedback.pushInfo("Target: {}.{}".format(database, schema))
        feedback.pushInfo("=================================")

        feedback.pushWarning(
            "IMPORTANT: QGIS Editor Tracking expressions are enabled; non-PK "
            "PostgreSQL column defaults are removed. PostgreSQL primary-key "
            "generation is preserved. Review heuristic domain matches before use."
        )

        return {
            self.OUTPUT_GPKG: gpkg_path,
        }


# PostgreSQL identifiers are limited to 63 bytes in standard PostgreSQL builds.
POSTGRESQL_IDENTIFIER_LIMIT = 63

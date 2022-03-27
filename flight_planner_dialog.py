# -*- coding: utf-8 -*-
"""
/***************************************************************************
 FlightPlannerDialog
                                 A QGIS plugin
 Flight Planner allows you to plan photogrammetric flight and control it.
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2019-09-22
        git sha              : $Format:%H$
        copyright            : (C) 2019 by Jakub Gruca
        email                : jakubmgruca@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
import os
import json
from math import sqrt, ceil, fabs, pi, atan2, atan

import processing
from osgeo import gdal
from pyproj import Transformer
from PyQt5.QtWidgets import QMessageBox, QInputDialog
from PyQt5.QtCore import pyqtSlot, QVariant, QThread
from qgis.PyQt import uic, QtWidgets
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsField,
    QgsFieldProxyModel,
    QgsMapLayerProxyModel,
    QgsProject
)

from .camera import Camera
from .worker import Worker
from .functions import (
    bounding_box_at_angle,
    projection_centres, line,
    transf_coord,
    minmaxheight,
    save_error
)

# Load .ui file so that PyQt can populate plugin
# with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'flight_planner_dialog_base.ui'))


class FlightPlannerDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(FlightPlannerDialog, self).__init__(parent)
        # Set up the user interface from Designer through FORM_CLASS.
        self.setupUi(self)
        self.tabWidgetBlock = True
        self.tabWidgetCorridor = False

        # Set up filters for ComboBoxes
        self.pcMapLayCombB.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.altitudeFieldComboBox.setFilters(QgsFieldProxyModel.Numeric)
        self.omegaFieldComboBox.setFilters(QgsFieldProxyModel.Numeric)
        self.phiFieldComboBox.setFilters(QgsFieldProxyModel.Numeric)
        self.kappaFieldComboBox.setFilters(QgsFieldProxyModel.Numeric)
        self.dtmMapLayCombB.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.aoiMapLayCombB.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.corMapLayCombB.setFilters(QgsMapLayerProxyModel.LineLayer)

        # Set up ComboBox of camera
        self.cameras_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'cameras.json')
        with open(self.cameras_file, 'r', encoding='utf-8') as file:
            self.cameras = [Camera(**camera) for camera in json.load(file)]
            self.combBcam.addItems([camera.name for camera in self.cameras])
        self.combBcam.setItemText(0, 'Select camera or type parameters')

    def startWorker_control(self, pnt_lay, h, o, p, k, f, s_sensor, s_along,
                            s_across, crs_vct, crs_rst, DTM, overlap_bool,
                            gsd_bool, footprint_bool, t):
        """Start worker for control module of plugin."""
        # Create a new worker instance
        worker = Worker(pointLayer=pnt_lay, hField=h, omegaField=o,
                        phiField=p, kappaField=k, focal=f,
                        size_sensor=s_sensor, size_along=s_along,
                        size_across=s_across, crsVectorLayer=crs_vct,
                        crsRasterLayer=crs_rst, DTM=DTM, overlap=overlap_bool,
                        gsd=gsd_bool, footprint=footprint_bool,
                        threshold=t)

        self.pBcancel.clicked.connect(worker.kill)
        # Start the worker in a new thread
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self.workerFinished)
        worker.error.connect(self.workerError)
        worker.progress.connect(self.progressBar.setValue)
        worker.enabled.connect(self.pBacceptControl.setEnabled)
        worker.enabled.connect(self.pBaccept.setEnabled)
        thread.started.connect(worker.run_control)
        thread.start()
        self.thread = thread
        self.worker = worker

    def startWorker_updateAltitude(self, pnt_lay, theta, dist, crs_vct, DTM, w,
                                   layer_pol, s=None, tabWidget=None, geom=None):
        """Start a worker for update altitude of flight in 'altitude for
        each strip' or 'terraing following' mode."""
        if self.rBstripsAltitude.isChecked():
            if tabWidget:
                worker = Worker(pointLayer=pnt_lay, theta=theta, distance=dist,
                                crsVectorLayer=crs_vct, DTM=DTM, height=w,
                                strips=s, tabWidg=tabWidget, LineRangeList=geom,
                                polygonLayer=layer_pol)
            else:
                worker = Worker(pointLayer=pnt_lay, theta=theta, distance=dist,
                                crsVectorLayer=crs_vct, DTM=DTM, height=w,
                                strips=s, tabWidg=tabWidget, Range=geom,
                                polygonLayer=layer_pol)

        elif self.rBterrainFollowing.isChecked():
            worker = Worker(pointLayer=pnt_lay, theta=theta, distance=dist,
                            crsVectorLayer=crs_vct, DTM=DTM, height=w,
                            polygonLayer=layer_pol)

        # Create a new worker instance                  
        self.pBcancel.clicked.connect(worker.kill)
        # Start the worker in a new thread
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self.workerFinished)
        worker.error.connect(self.workerError)
        worker.progress.connect(self.progressBar.setValue)
        worker.enabled.connect(self.pBaccept.setEnabled)
        worker.enabled.connect(self.pBacceptControl.setEnabled)

        if self.rBstripsAltitude.isChecked():
            thread.started.connect(worker.run_altitudeStrip)
        elif self.rBterrainFollowing.isChecked():
            thread.started.connect(worker.run_followingTerrain)

        thread.start()
        self.thread = thread
        self.worker = worker

    def workerFinished(self, result):
        # clean up the worker and thread
        self.worker.deleteLater()
        self.thread.quit()
        self.thread.wait()
        self.thread.deleteLater()
        if result is not None:
            # report the result
            QgsProject.instance().addMapLayers(result)
        else:
            # notify the user that something went wrong
            print('Something went wrong!')

    def workerError(self, e, exception_string):
        print(f'Worker thread raised an exception: {exception_string}')
        save_error()
        QMessageBox.about(self, 'Error', 'See error log file in plugin folder')

    def on_pBcancel_clicked(self):
        pass

    def on_progressBar_valueChanged(self):
        pass

    def on_combBcam_highlighted(self):
        camera_names = [camera.name for camera in self.cameras]
        if self.combBcam.currentText() == 'Select camera or type parameters':
            self.combBcam.removeItem(self.combBcam.currentIndex())
            self.combBcam.insertItem(self.combBcam.currentIndex(),
                                     camera_names[self.combBcam.currentIndex()])

    def on_combBcam_activated(self, i):
        if isinstance(i, int):
            self.lEfocal.setText(self.cameras[i].focal_length)
            self.lEsensor.setText(self.cameras[i].sensor_size)
            self.lEalong.setText(self.cameras[i].pixels_along_track)
            self.lEacross.setText(self.cameras[i].pixels_across_track)

    def on_lEfocal_textChanged(self):
        pass

    def on_lEgsd_textChanged(self):
        pass

    def on_lEsensor_textChanged(self):
        pass

    def on_lEalong_textChanged(self):
        pass

    def on_lEacross_textChanged(self):
        pass

    def on_lEmaxH_textChanged(self):
        pass

    def on_lEminH_textChanged(self):
        pass

    def on_lEp_textChanged(self):
        pass

    def on_lEq_textChanged(self):
        pass

    def on_dSpinBoxBuffer_valueChanged(self):
        pass

    def on_dSpinBoxThreshold_valueChanged(self):
        pass

    def on_rBoneAltitude_toggled(self):
        pass

    def on_rBstripsAltitude_toggled(self):
        pass

    def on_rBterrainFollowing_toggled(self):
        pass

    def on_cBoverlapPict_stateChanged(self):
        pass

    def on_cBfootprint_stateChanged(self):
        if self.cBfootprint.isChecked():
            self.dSpinBoxThreshold.setEnabled(True)
            self.label_iteration.setEnabled(True)
            self.label_threshold.setEnabled(True)
        else:
            self.dSpinBoxThreshold.setEnabled(False)
            self.label_iteration.setEnabled(False)
            self.label_threshold.setEnabled(False)

    def on_cBgsdMap_stateChanged(self):
        pass

    def on_cBincreaseOverlap_stateChanged(self):
        try:
            focal = float(self.lEfocal.text().replace(',', '.'))
            gsd = float(self.lEgsd.text().replace(',', '.'))
            sensor = float(self.lEsensor.text().replace(',', '.'))
            max_h = float(self.lEmaxH.text().replace(',', '.'))
            min_h = float(self.lEminH.text().replace(',', '.'))
            p0 = float(self.lEp.text().replace(',', '.'))
            q0 = float(self.lEq.text().replace(',', '.'))
            W = ((gsd * 10) / (sensor / 1000) * focal) / 1000
            if self.cBincreaseOverlap.isChecked():
                # remember old values of overlap p, sidelap q
                self.p0_prev = float(self.lEp.text().replace(',', '.'))
                self.q0_prev = float(self.lEq.text().replace(',', '.'))
                # new values of overlap p, sidelap q
                self.p = p0 / 100 + 0.5 * ((max_h - min_h) / 2) / W
                self.q = q0 / 100 + 0.7 * ((max_h - min_h) / 2) / W
            else:
                self.p = self.p0_prev / 100
                self.q = self.q0_prev / 100
        except ValueError:
            QMessageBox.about(self, 'Error', 'Type float numbers')
        else:
            self.lEp.setText(str(round(self.p * 100, 1)))
            self.lEq.setText(str(round(self.q * 100, 1)))

    def on_sBmultipleBase(self):
        pass

    def on_pcMapLayCombB_layerChanged(self):
        try:
            if self.pcMapLayCombB.currentLayer():
                proj_cent_layer = self.pcMapLayCombB.currentLayer()
                self.crs_vct_ctrl = proj_cent_layer.sourceCrs().authid()
                crs = QgsCoordinateReferenceSystem(self.crs_vct_ctrl)
                if crs.isGeographic():
                    QMessageBox.about(self, 'Error', 'CRS of layer cannot be' \
                                      + 'geographic')
                    self.altitudeFieldComboBox.setLayer(None)
                    self.omegaFieldComboBox.setLayer(None)
                    self.phiFieldComboBox.setLayer(None)
                    self.kappaFieldComboBox.setLayer(None)
                else:
                    self.altitudeFieldComboBox.setLayer(proj_cent_layer)
                    self.omegaFieldComboBox.setLayer(proj_cent_layer)
                    self.phiFieldComboBox.setLayer(proj_cent_layer)
                    self.kappaFieldComboBox.setLayer(proj_cent_layer)
        except:
            QMessageBox.about(self, 'Error', 'See error log file in plugin' \
                              + 'folder')
            save_error()

    def on_hFieldComboBox_fieldChanged(self):
        pass

    def on_omegaFieldComboBox_fieldChanged(self):
        pass

    def on_phiFieldComboBox_fieldChanged(self):
        pass

    def on_kappaFieldComboBox_fieldChanged(self):
        pass

    @pyqtSlot()
    def on_pBgetHeights_clicked(self):
        try:
            if self.tabWidgetBlock:
                h_min, h_max = minmaxheight(self.AreaOfInterest, self.DTM)
            else:
                # setting minimum buffer size to be able to get heights
                if self.crs_rst != self.crs_vct:
                    transf_rst_vct = Transformer.from_crs(self.crs_rst,
                                                          self.crs_vct,
                                                          always_xy=True)
                else:
                    transf_rst_vct = None
                g_rst = self.raster.GetGeoTransform()
                pix_width = g_rst[1]
                pix_height = -g_rst[5]
                uplx = g_rst[0]
                uply = g_rst[3]
                uplx_n = uplx + pix_width
                uply_n = uply + pix_height
                xo, yo = transf_coord(transf_rst_vct, uplx, uply)
                xo1, yo1 = transf_coord(transf_rst_vct, uplx_n, uply_n)
                min_buff_size = max(ceil(fabs(xo1 - xo)), ceil(fabs(yo1 - yo)))
                self.dSpinBoxBuffer.setMinimum(min_buff_size / 2)
                buffLine = processing.run("native:buffer",
                                          {'INPUT': self.pathLine,
                                           'DISTANCE': self.dSpinBoxBuffer.value(),
                                           'SEGMENTS': 5, 'END_CAP_STYLE': 0, 'JOIN_STYLE': 0,
                                           'MITER_LIMIT': 2, 'DISSOLVE': False,
                                           'OUTPUT': 'TEMPORARY_OUTPUT'})
                self.bufferedLine = buffLine['OUTPUT']
                h_min, h_max = minmaxheight(self.bufferedLine, self.DTM)
        except:
            QMessageBox.about(self, 'Error', 'Get heights from DTM failed')
            save_error()
        else:
            self.lEminH.setText(str(h_min))
            self.lEmaxH.setText(str(h_max))

    def on_corMapLayCombB_layerChanged(self):
        self.CorLine = self.corMapLayCombB.currentLayer()
        if not self.CorLine == None:
            self.crs_vct = self.CorLine.sourceCrs().authid()
            if QgsCoordinateReferenceSystem(self.crs_vct).isGeographic():
                QMessageBox.about(self, 'Error',
                                  'CRS of layer cannot be geographic')
                self.corMapLayCombB.setLayer(None)
            else:
                self.pathLine = self.CorLine.dataProvider().dataSourceUri()

    def on_aoiMapLayCombB_layerChanged(self):
        self.AreaOfInterest = self.aoiMapLayCombB.currentLayer()
        if not self.AreaOfInterest == None:
            self.crs_vct = self.AreaOfInterest.sourceCrs().authid()
            if QgsCoordinateReferenceSystem(self.crs_vct).isGeographic():
                QMessageBox.about(self, 'Error',
                                  'CRS of layer cannot be geographic')
                self.aoiMapLayCombB.setLayer(None)
            else:
                features = self.AreaOfInterest.getFeatures()
                for feature in features:
                    self.geom_AoI = feature.geometry()

    def on_dtmMapLayCombB_layerChanged(self):
        if self.dtmMapLayCombB.currentLayer():
            self.DTM = self.dtmMapLayCombB.currentLayer()
            self.crs_rst = self.DTM.crs().authid()
            pathDTM = self.DTM.source()
            self.raster = gdal.Open(pathDTM)
            self.rBstripsAltitude.setEnabled(True)
            self.rBterrainFollowing.setEnabled(True)
            self.pBgetHeights.setEnabled(True)

    def on_tabWidget_currentChanged(self):
        if self.tabWidget.currentIndex() == 0:
            self.tabWidgetBlock = True
            self.tabWidgetCorridor = False
        else:
            self.tabWidgetBlock = False
            self.tabWidgetCorridor = True

    def on_dial_valueChanged(self):
        if self.dial.value() > 180:
            self.sBdirection.setValue(self.dial.value() - 180)
        else:
            self.sBdirection.setValue(self.dial.value() + 180)

    def on_sBdirection_valueChanged(self):
        if self.sBdirection.value() > 180:
            self.dial.setValue(self.sBdirection.value() - 180)
        else:
            self.dial.setValue(self.sBdirection.value() + 180)

    def on_sBexceedingExtremeStrips(self):
        pass

    @pyqtSlot()
    def on_pBaccept_clicked(self):
        """Push Button to make a flight plan."""
        try:
            focal = float(self.lEfocal.text().replace(',', '.'))
            gsd = float(self.lEgsd.text().replace(',', '.'))
            sensor = float(self.lEsensor.text().replace(',', '.'))
            along = int(self.lEalong.text())
            across = int(self.lEacross.text())
            max_h = float(self.lEmaxH.text().replace(',', '.'))
            min_h = float(self.lEminH.text().replace(',', '.'))
            p0 = float(self.lEp.text().replace(',', '.'))
            q0 = float(self.lEq.text().replace(',', '.'))
            mult_base = self.sBmultipleBase.value()
            x_percent = self.sBexceedingExtremeStrips.value()

            # flight height above mean terrain height
            w = ((gsd * 10) / (sensor / 1000) * focal) / 1000
            mean_h = (max_h + min_h) / 2
            # above sea level flight height
            w0 = w + mean_h
            if not self.cBincreaseOverlap.isChecked():
                self.p = p0 / 100
                self.q = q0 / 100
            # image length along and across flight direction[m]
            len_along = along * gsd / 100
            len_across = across * gsd / 100
            # longitudinal base Bx, transverse base By
            Bx = len_along * (1 - self.p)
            By = len_across * (1 - self.q)
            strip = 0
            photo = 0

            if self.tabWidgetBlock:
                angle = 90 - self.sBdirection.value()
                if 90 - self.sBdirection.value() < 0:
                    angle = 90 - self.sBdirection.value() + 360
                # bounding box equotations and dimensions Dx, Dy
                a, b, a2, b2, Dx, Dy = bounding_box_at_angle(angle,
                                                             self.geom_AoI)
                pc_lay, photo_lay, s_nr, p_nr = projection_centres(
                    angle, self.geom_AoI, self.crs_vct, a, b, a2, b2, Dx, Dy,
                    Bx, By, len_along, len_across, x_percent, mult_base, w0,
                    strip, photo)

            elif self.tabWidgetCorridor:
                exploded_lines = processing.run("native:explodelines",
                                                {'INPUT': self.pathLine,
                                                 'OUTPUT': 'TEMPORARY_OUTPUT'})
                exp_lines = exploded_lines['OUTPUT']
                # buffer for each exp_lines
                buffered_exp_lines = processing.run("native:buffer",
                                                    {'INPUT': exp_lines, 'DISTANCE': self.dSpinBoxBuffer.value(),
                                                     'SEGMENTS': 5, 'END_CAP_STYLE': 0, 'JOIN_STYLE': 0,
                                                     'MITER_LIMIT': 2, 'DISSOLVE': False,
                                                     'OUTPUT': 'TEMPORARY_OUTPUT'})
                buff_exp_lines = buffered_exp_lines['OUTPUT']
                feats_exp_lines = exp_lines.getFeatures()
                pc_lay_list = []
                photo_lay_list = []
                line_buf_list = []

                # building projection centres and photos layer for each line
                for feat_exp in feats_exp_lines:
                    x_start = feat_exp.geometry().asPolyline()[0].x()
                    y_start = feat_exp.geometry().asPolyline()[0].y()
                    x_end = feat_exp.geometry().asPolyline()[1].x()
                    y_end = feat_exp.geometry().asPolyline()[1].y()
                    # equation of corridor line
                    a_line, b_line = line(y_start, y_end, x_start, x_end)
                    angle = atan(a_line) * 180 / pi

                    if angle < 0:
                        angle = angle + 180

                    featbuff_exp = buff_exp_lines.getFeature(feat_exp.id())
                    # geometry object of line buffer
                    geom_line_buf = featbuff_exp.geometry()
                    line_buf_list.append(geom_line_buf)
                    a, b, a2, b2, Dx, Dy = bounding_box_at_angle(angle,
                                                                 geom_line_buf)
                    # projection centres layer and photos layer for given line
                    pc_lay, photo_lay, s_nr, p_nr = projection_centres(
                        angle, geom_line_buf, self.crs_vct, a, b, a2, b2, Dx,
                        Dy, Bx, By, len_along, len_across, x_percent,
                        mult_base, w0, strip, photo)
                    # adding helping field for function 'alt. for each strip'
                    pc_lay.startEditing()
                    pc_lay.addAttribute(QgsField("BuffNr", QVariant.Int))
                    pc_lay.selectAll()

                    for f in range(min(pc_lay.selectedFeatureIds()) - 1, \
                                   max(pc_lay.selectedFeatureIds()) + 1):
                        pc_lay.changeAttributeValue(f, 8, feat_exp.id())

                    pc_lay.commitChanges()
                    pc_lay_list.append(pc_lay)
                    photo_lay_list.append(photo_lay)
                    strip = s_nr
                    photo = p_nr

                # merging results for every line
                merged_pnt_lay = processing.run("native:mergevectorlayers",
                                                {'LAYERS': pc_lay_list,
                                                 'CRS': None, 'OUTPUT': 'TEMPORARY_OUTPUT'})
                pc_lay = merged_pnt_lay['OUTPUT']
                merged_poly_lay = processing.run("native:mergevectorlayers",
                                                 {'LAYERS': photo_lay_list, 'CRS': None,
                                                  'OUTPUT': 'TEMPORARY_OUTPUT'})
                photo_lay = merged_poly_lay['OUTPUT']
            s = int(pc_lay.maximumValue(0))
            theta = fabs(atan2(len_across / 2, len_along / 2))
            dist = sqrt((len_along / 2) ** 2 + (len_across / 2) ** 2)
        except:
            QMessageBox.about(self, 'Error', 'make sure you have provided the' \
                              + ' data (AoI, camera parameters etc.) correctly')
            save_error()
        else:
            # thread for 'altitude for each strip' option
            if self.rBstripsAltitude.isChecked():  # or self.rBterrainFollowing.isChecked()
                if self.tabWidgetCorridor:
                    self.startWorker_updateAltitude(pc_lay, theta, dist,
                                                    self.crs_vct, self.DTM, w,
                                                    photo_lay, s,
                                                    self.tabWidgetCorridor,
                                                    line_buf_list)
                else:
                    self.startWorker_updateAltitude(pc_lay, theta, dist,
                                                    self.crs_vct, self.DTM, w,
                                                    photo_lay, s,
                                                    self.tabWidgetCorridor,
                                                    self.geom_AoI)
                self.pBaccept.setEnabled(False)
                self.pBacceptControl.setEnabled(False)

            elif self.rBterrainFollowing.isChecked():
                # thread for 'terraing following' option
                self.startWorker_updateAltitude(pc_lay, theta, dist,
                                                self.crs_vct, self.DTM,
                                                w, photo_lay)
                self.pBaccept.setEnabled(False)
                self.pBacceptControl.setEnabled(False)

            else:
                # delete redundant fields
                pc_lay.startEditing()
                pc_lay.deleteAttributes([8, 9, 10])
                pc_lay.commitChanges()
                photo_lay.startEditing()
                photo_lay.deleteAttributes([2, 3])
                photo_lay.commitChanges()

                # change layers style
                renderer = photo_lay.renderer()
                symbol = renderer.symbol()
                prop = {'color': '200,200,200,30', 'color_border': '#000000',
                        'width_border': '0.2'}
                my_symbol = symbol.createSimple(prop)
                renderer.setSymbol(my_symbol)
                photo_lay.triggerRepaint()
                photo_lay.setName('photos')
                pc_lay.setName('projection centres')

                # add layers to canvas
                QgsProject.instance().addMapLayer(photo_lay)
                QgsProject.instance().addMapLayer(pc_lay)

    @pyqtSlot()
    def on_pBacceptControl_clicked(self):
        """Push Button to execute all control activites."""
        try:
            # read all necessary parameters
            proj_centres = self.pcMapLayCombB.currentLayer()
            h_field = self.altitudeFieldComboBox.currentField()
            o_field = self.omegaFieldComboBox.currentField()
            p_field = self.phiFieldComboBox.currentField()
            k_field = self.kappaFieldComboBox.currentField()
            focal = float(self.lEfocal.text().replace(',', '.')) / 1000  # [m]
            size_sensor = float(self.lEsensor.text().replace(',', '.')) / 1000000  # [m]
            size_along = int(self.lEalong.text())
            size_across = int(self.lEacross.text())
            threshold = self.dSpinBoxThreshold.value()
            if not self.crs_rst or not h_field or not o_field or not p_field \
                    or not k_field:
                raise NameError
        except (AttributeError, ValueError, NameError):
            QMessageBox.about(self, 'Error', 'Make sure you have provided the'
                                             ' data (DTM, projection centers,'
                                             ' camera parameters) correctly')
            save_error()
        else:
            # start worker to move hard task into a separate thread
            self.startWorker_control(pnt_lay=proj_centres, h=h_field,
                                     o=o_field, p=p_field, k=k_field, f=focal,
                                     s_sensor=size_sensor, s_along=size_along,
                                     s_across=size_across, crs_vct=self.crs_vct_ctrl,
                                     crs_rst=self.crs_rst, DTM=self.raster,
                                     overlap_bool=self.cBoverlapPict.isChecked(),
                                     gsd_bool=self.cBgsdMap.isChecked(),
                                     footprint_bool=self.cBfootprint.isChecked(),
                                     t=threshold)
            # disable GUI elements to prevent thread from starting
            # a second time
            self.pBacceptControl.setEnabled(False)
            self.pBaccept.setEnabled(False)

    @pyqtSlot()
    def on_pBaddCamera_clicked(self):
        """Push Button to add camera to camera list."""
        try:
            camera_name, pressed_ok = QInputDialog.getText(self, 'Save camera',
                                                        'Enter camera name:')
            if pressed_ok:
                new_camera = Camera(camera_name,
                                    self.lEfocal.text(),
                                    self.lEsensor.text(),
                                    self.lEalong.text(),
                                    self.lEacross.text())
                new_camera.save()
                self.cameras.append(new_camera)
                self.combBcam.addItem(new_camera.name)
                self.combBcam.setCurrentText(self.cameras[-1].name)
        except:
            QMessageBox.about(self, 'Error', 'Saving camera failed')
            save_error()

    @pyqtSlot()
    def on_pBdelCamera_clicked(self):
        """Push Button to delete camera from camera list."""
        try:
            camera_names = [camera.name for camera in self.cameras]
            option, pressed = QInputDialog.getItem(None, "Delete camera",
                                            "Select camera to delete:",
                                            camera_names, 0, False)
            if pressed:
                selected_camera = next(camera for camera in self.cameras if camera.name == option)
                selected_camera.delete()
                self.cameras.remove(selected_camera)
                selected_camera_index = self.combBcam.findText(selected_camera.name)
                self.combBcam.removeItem(selected_camera_index)

                if self.combBcam.currentText():
                    self.lEfocal.setText(self.cameras[self.combBcam.currentIndex()].focal_length)
                    self.lEsensor.setText(self.cameras[self.combBcam.currentIndex()].sensor_size)
                    self.lEalong.setText(self.cameras[self.combBcam.currentIndex()].pixels_along_track)
                    self.lEacross.setText(self.cameras[self.combBcam.currentIndex()].pixels_across_track)
                else:
                    self.lEfocal.setText('')
                    self.lEsensor.setText('')
                    self.lEalong.setText('')
                    self.lEacross.setText('')
        except:
            QMessageBox.about(self, 'Error', 'Deleting camera failed')
            save_error()
